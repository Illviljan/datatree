from __future__ import annotations

import functools
from itertools import repeat
from textwrap import dedent
from typing import TYPE_CHECKING, Callable, Tuple

from xarray import DataArray, Dataset

from .iterators import LevelOrderIter
from .treenode import NodePath, TreeNode

if TYPE_CHECKING:
    from .datatree import DataTree


class TreeIsomorphismError(ValueError):
    """Error raised if two tree objects do not share the same node structure."""

    pass


def check_isomorphic(
    a: DataTree,
    b: DataTree,
    require_names_equal: bool = False,
    check_from_root: bool = True,
):
    """
    Check that two trees have the same structure, raising an error if not.

    Does not compare the actual data in the nodes.

    By default this function only checks that subtrees are isomorphic, not the entire tree above (if it exists).
    Can instead optionally check the entire trees starting from the root, which will ensure all

    Can optionally check if corresponding nodes should have the same name.

    Parameters
    ----------
    a : DataTree
    b : DataTree
    require_names_equal : Bool
        Whether or not to also check that each node has the same name as its counterpart.
    check_from_root : Bool
        Whether or not to first traverse to the root of the trees before checking for isomorphism.
        If a & b have no parents then this has no effect.

    Raises
    ------
    TypeError
        If either a or b are not tree objects.
    TreeIsomorphismError
        If a and b are tree objects, but are not isomorphic to one another.
        Also optionally raised if their structure is isomorphic, but the names of any two
        respective nodes are not equal.
    """

    if not isinstance(a, TreeNode):
        raise TypeError(f"Argument `a` is not a tree, it is of type {type(a)}")
    if not isinstance(b, TreeNode):
        raise TypeError(f"Argument `b` is not a tree, it is of type {type(b)}")

    if check_from_root:
        a = a.root
        b = b.root

    diff = diff_treestructure(a, b, require_names_equal=require_names_equal)

    if diff:
        raise TreeIsomorphismError("DataTree objects are not isomorphic:\n" + diff)


def diff_treestructure(a: DataTree, b: DataTree, require_names_equal: bool) -> str:
    """
    Return a summary of why two trees are not isomorphic.
    If they are isomorphic return an empty string.
    """

    # Walking nodes in "level-order" fashion means walking down from the root breadth-first.
    # Checking for isomorphism by walking in this way implicitly assumes that the tree is an ordered tree
    # (which it is so long as children are stored in a tuple or list rather than in a set).
    for node_a, node_b in zip(LevelOrderIter(a), LevelOrderIter(b)):
        path_a, path_b = node_a.path, node_b.path

        if require_names_equal:
            if node_a.name != node_b.name:
                diff = dedent(
                    f"""\
                Node '{path_a}' in the left object has name '{node_a.name}'
                Node '{path_b}' in the right object has name '{node_b.name}'"""
                )
                return diff

        if len(node_a.children) != len(node_b.children):
            diff = dedent(
                f"""\
                Number of children on node '{path_a}' of the left object: {len(node_a.children)}
                Number of children on node '{path_b}' of the right object: {len(node_b.children)}"""
            )
            return diff

    return ""


def map_over_subtree(func: Callable) -> Callable:
    """
    Decorator which turns a function which acts on (and returns) Datasets into one which acts on and returns DataTrees.

    Applies a function to every dataset in one or more subtrees, returning new trees which store the results.

    The function will be applied to any non-empty dataset stored in any of the nodes in the trees. The returned trees
    will have the same structure as the supplied trees.

    `func` needs to return one Datasets, DataArrays, or None in order to be able to rebuild the subtrees after
    mapping, as each result will be assigned to its respective node of a new tree via `DataTree.__setitem__`. Any
    returned value that is one of these types will be stacked into a separate tree before returning all of them.

    The trees passed to the resulting function must all be isomorphic to one another. Their nodes need not be named
    similarly, but all the output trees will have nodes named in the same way as the first tree passed.

    Parameters
    ----------
    func : callable
        Function to apply to datasets with signature:

        `func(*args, **kwargs) -> Union[Dataset, Iterable[Dataset]]`.

        (i.e. func must accept at least one Dataset and return at least one Dataset.)
        Function will not be applied to any nodes without datasets.
    *args : tuple, optional
        Positional arguments passed on to `func`. If DataTrees any data-containing nodes will be converted to Datasets
        via .ds .
    **kwargs : Any
        Keyword arguments passed on to `func`. If DataTrees any data-containing nodes will be converted to Datasets
        via .ds .

    Returns
    -------
    mapped : callable
        Wrapped function which returns one or more tree(s) created from results of applying ``func`` to the dataset at
        each node.

    See also
    --------
    DataTree.map_over_subtree
    DataTree.map_over_subtree_inplace
    DataTree.subtree
    """

    # TODO examples in the docstring

    # TODO inspect function to work out immediately if the wrong number of arguments were passed for it?

    @functools.wraps(func)
    def _map_over_subtree(*args, **kwargs) -> DataTree | Tuple[DataTree, ...]:
        """Internal function which maps func over every node in tree, returning a tree of the results."""
        from .datatree import DataTree

        parallel = True
        if parallel:
            import dask

            func_ = dask.delayed(func)
        else:
            func_ = func

        all_tree_inputs = [a for a in args if isinstance(a, DataTree)] + [
            a for a in kwargs.values() if isinstance(a, DataTree)
        ]

        if len(all_tree_inputs) > 0:
            first_tree, *other_trees = all_tree_inputs
        else:
            raise TypeError("Must pass at least one tree object")

        for other_tree in other_trees:
            # isomorphism is transitive so this is enough to guarantee all trees are mutually isomorphic
            check_isomorphic(
                first_tree,
                other_tree,
                require_names_equal=False,
                check_from_root=False,
            )

        # Walk all trees simultaneously, applying func to all nodes that lie in same position in different trees
        # We don't know which arguments are DataTrees so we zip all arguments together as iterables
        # Store tuples of results in a dict because we don't yet know how many trees we need to rebuild to return
        out_data_objects = {}
        args_as_tree_length_iterables = [
            a.subtree if isinstance(a, DataTree) else repeat(a) for a in args
        ]
        n_args = len(args_as_tree_length_iterables)
        kwargs_as_tree_length_iterables = {
            k: v.subtree if isinstance(v, DataTree) else repeat(v)
            for k, v in kwargs.items()
        }
        for node_of_first_tree, *all_node_args in zip(
            first_tree.subtree,
            *args_as_tree_length_iterables,
            *list(kwargs_as_tree_length_iterables.values()),
        ):
            node_args_as_datasets = [
                a.to_dataset() if isinstance(a, DataTree) else a
                for a in all_node_args[:n_args]
            ]
            node_kwargs_as_datasets = dict(
                zip(
                    [k for k in kwargs_as_tree_length_iterables.keys()],
                    [
                        v.to_dataset() if isinstance(v, DataTree) else v
                        for v in all_node_args[n_args:]
                    ],
                )
            )

            # Now we can call func on the data in this particular set of corresponding nodes
            results = (
                func_(*node_args_as_datasets, **node_kwargs_as_datasets)
                if not node_of_first_tree.is_empty
                else None
            )

            # TODO implement mapping over multiple trees in-place using if conditions from here on?
            out_data_objects[node_of_first_tree.path] = results

        if parallel:
            keys, values = dask.compute(
                [k for k in out_data_objects.keys()],
                [v for v in out_data_objects.values()],
            )
            out_data_objects = {k: v for k, v in zip(keys, values)}

        # Find out how many return values we received
        num_return_values = _check_all_return_values(out_data_objects)

        # Reconstruct 1+ subtrees from the dict of results, by filling in all nodes of all result trees
        original_root_path = first_tree.path
        result_trees = []
        for i in range(num_return_values):
            out_tree_contents = {}
            for n in first_tree.subtree:
                p = n.path
                if p in out_data_objects.keys():
                    if isinstance(out_data_objects[p], tuple):
                        output_node_data = out_data_objects[p][i]
                    else:
                        output_node_data = out_data_objects[p]
                else:
                    output_node_data = None

                # Discard parentage so that new trees don't include parents of input nodes
                relative_path = str(NodePath(p).relative_to(original_root_path))
                relative_path = "/" if relative_path == "." else relative_path
                out_tree_contents[relative_path] = output_node_data

            new_tree = DataTree.from_dict(
                out_tree_contents,
                name=first_tree.name,
            )
            result_trees.append(new_tree)

        # If only one result then don't wrap it in a tuple
        if len(result_trees) == 1:
            return result_trees[0]
        else:
            return tuple(result_trees)

    return _map_over_subtree


def _check_single_set_return_values(path_to_node, obj):
    """Check types returned from single evaluation of func, and return number of return values received from func."""
    if isinstance(obj, (Dataset, DataArray)):
        return 1
    elif isinstance(obj, tuple):
        for r in obj:
            if not isinstance(r, (Dataset, DataArray)):
                raise TypeError(
                    f"One of the results of calling func on datasets on the nodes at position {path_to_node} is "
                    f"of type {type(r)}, not Dataset or DataArray."
                )
        return len(obj)
    else:
        raise TypeError(
            f"The result of calling func on the node at position {path_to_node} is of type {type(obj)}, not "
            f"Dataset or DataArray, nor a tuple of such types."
        )


def _check_all_return_values(returned_objects):
    """Walk through all values returned by mapping func over subtrees, raising on any invalid or inconsistent types."""

    if all(r is None for r in returned_objects.values()):
        raise TypeError(
            "Called supplied function on all nodes but found a return value of None for"
            "all of them."
        )

    result_data_objects = [
        (path_to_node, r)
        for path_to_node, r in returned_objects.items()
        if r is not None
    ]

    if len(result_data_objects) == 1:
        # Only one node in the tree: no need to check consistency of results between nodes
        path_to_node, result = result_data_objects[0]
        num_return_values = _check_single_set_return_values(path_to_node, result)
    else:
        prev_path, _ = result_data_objects[0]
        prev_num_return_values, num_return_values = None, None
        for path_to_node, obj in result_data_objects[1:]:
            num_return_values = _check_single_set_return_values(path_to_node, obj)

            if (
                num_return_values != prev_num_return_values
                and prev_num_return_values is not None
            ):
                raise TypeError(
                    f"Calling func on the nodes at position {path_to_node} returns {num_return_values} separate return "
                    f"values, whereas calling func on the nodes at position {prev_path} instead returns "
                    f"{prev_num_return_values} separate return values."
                )

            prev_path, prev_num_return_values = path_to_node, num_return_values

    return num_return_values
