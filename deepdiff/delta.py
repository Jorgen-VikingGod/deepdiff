import copy
import logging
from typing import List, Dict, IO, Callable, Set, Union, Optional
from functools import partial
from collections.abc import Mapping
from copy import deepcopy
from ordered_set import OrderedSet
from deepdiff import DeepDiff
from deepdiff.serialization import pickle_load, pickle_dump
from deepdiff.helper import (
    strings, short_repr, numbers,
    np_ndarray, np_array_factory, numpy_dtypes, get_doc,
    not_found, numpy_dtype_string_to_type, dict_,
    Opcode, FlatDeltaRow, UnkownValueCode,
)
from deepdiff.path import (
    _path_to_elements, _get_nested_obj, _get_nested_obj_and_force,
    GET, GETATTR, parse_path, stringify_path,
)
from deepdiff.anyset import AnySet


logger = logging.getLogger(__name__)


VERIFICATION_MSG = 'Expected the old value for {} to be {} but it is {}. Error found on: {}'
ELEM_NOT_FOUND_TO_ADD_MSG = 'Key or index of {} is not found for {} for setting operation.'
TYPE_CHANGE_FAIL_MSG = 'Unable to do the type change for {} from to type {} due to {}'
VERIFY_BIDIRECTIONAL_MSG = ('You have applied the delta to an object that has '
                            'different values than the original object the delta was made from.')
FAIL_TO_REMOVE_ITEM_IGNORE_ORDER_MSG = 'Failed to remove index[{}] on {}. It was expected to be {} but got {}'
DELTA_NUMPY_OPERATOR_OVERRIDE_MSG = (
    'A numpy ndarray is most likely being added to a delta. '
    'Due to Numpy override the + operator, you can only do: delta + ndarray '
    'and NOT ndarray + delta')
BINIARY_MODE_NEEDED_MSG = "Please open the file in the binary mode and pass to Delta by passing 'b' in open(..., 'b'): {}"
DELTA_AT_LEAST_ONE_ARG_NEEDED = 'At least one of the diff, delta_path or delta_file arguments need to be passed.'
INVALID_ACTION_WHEN_CALLING_GET_ELEM = 'invalid action of {} when calling _get_elem_and_compare_to_old_value'
INVALID_ACTION_WHEN_CALLING_SIMPLE_SET_ELEM = 'invalid action of {} when calling _simple_set_elem_value'
INVALID_ACTION_WHEN_CALLING_SIMPLE_DELETE_ELEM = 'invalid action of {} when calling _simple_set_elem_value'
UNABLE_TO_GET_ITEM_MSG = 'Unable to get the item at {}: {}'
UNABLE_TO_GET_PATH_MSG = 'Unable to get the item at {}'
INDEXES_NOT_FOUND_WHEN_IGNORE_ORDER = 'Delta added to an incompatible object. Unable to add the following items at the specific indexes. {}'
NUMPY_TO_LIST = 'NUMPY_TO_LIST'
NOT_VALID_NUMPY_TYPE = "{} is not a valid numpy type."

doc = get_doc('delta.rst')


class DeltaError(ValueError):
    """
    Delta specific errors
    """
    pass


class DeltaNumpyOperatorOverrideError(ValueError):
    """
    Delta Numpy Operator Override Error
    """
    pass


class Delta:

    __doc__ = doc

    def __init__(
        self,
        diff: Union[DeepDiff, Mapping, str, bytes, None]=None,
        delta_path: Optional[str]=None,
        delta_file: Optional[IO]=None,
        delta_diff: Optional[dict]=None,
        flat_dict_list: Optional[List[Dict]]=None,
        flat_rows_list: Optional[List[FlatDeltaRow]]=None,
        deserializer: Callable=pickle_load,
        log_errors: bool=True,
        mutate: bool=False,
        raise_errors: bool=False,
        safe_to_import: Optional[Set[str]]=None,
        serializer: Callable=pickle_dump,
        verify_symmetry: Optional[bool]=None,
        bidirectional: bool=False,
        always_include_values: bool=False,
        iterable_compare_func_was_used: Optional[bool]=None,
        force: bool=False,
    ):
        # for pickle deserializer:
        if hasattr(deserializer, '__code__') and 'safe_to_import' in set(deserializer.__code__.co_varnames):
            _deserializer = deserializer
        else:
            def _deserializer(obj, safe_to_import=None):
                result = deserializer(obj)
                if result.get('_iterable_opcodes'):
                    _iterable_opcodes = {}
                    for path, op_codes in result['_iterable_opcodes'].items():
                        _iterable_opcodes[path] = []
                        for op_code in op_codes:
                            _iterable_opcodes[path].append(
                                Opcode(
                                    **op_code
                                )
                            )
                    result['_iterable_opcodes'] = _iterable_opcodes
                return result


        self._reversed_diff = None

        if verify_symmetry is not None:
            logger.warning(
                "DeepDiff Deprecation: use bidirectional instead of verify_symmetry parameter."
            )
            bidirectional = verify_symmetry

        self.bidirectional = bidirectional
        if bidirectional:
            self.always_include_values = True  # We need to include the values in bidirectional deltas
        else:
            self.always_include_values = always_include_values

        if diff is not None:
            if isinstance(diff, DeepDiff):
                self.diff = diff._to_delta_dict(directed=not bidirectional, always_include_values=self.always_include_values)
            elif isinstance(diff, Mapping):
                self.diff = diff
            elif isinstance(diff, strings):
                self.diff = _deserializer(diff, safe_to_import=safe_to_import)
        elif delta_path:
            with open(delta_path, 'rb') as the_file:
                content = the_file.read()
            self.diff = _deserializer(content, safe_to_import=safe_to_import)
        elif delta_diff:
            self.diff = delta_diff
        elif delta_file:
            try:
                content = delta_file.read()
            except UnicodeDecodeError as e:
                raise ValueError(BINIARY_MODE_NEEDED_MSG.format(e)) from None
            self.diff = _deserializer(content, safe_to_import=safe_to_import)
        elif flat_dict_list:
            # Use copy to preserve original value of flat_dict_list in calling module
            self.diff = self._from_flat_dicts(copy.deepcopy(flat_dict_list))
        elif flat_rows_list:
            self.diff = self._from_flat_rows(copy.deepcopy(flat_rows_list))
        else:
            raise ValueError(DELTA_AT_LEAST_ONE_ARG_NEEDED)

        self.mutate = mutate
        self.raise_errors = raise_errors
        self.log_errors = log_errors
        self._numpy_paths = self.diff.get('_numpy_paths', False)
        # When we create the delta from a list of flat dictionaries, details such as iterable_compare_func_was_used get lost.
        # That's why we allow iterable_compare_func_was_used to be explicitly set.
        self._iterable_compare_func_was_used = self.diff.get('_iterable_compare_func_was_used', iterable_compare_func_was_used)
        self.serializer = serializer
        self.deserializer = deserializer
        self.force = force
        if force:
            self.get_nested_obj = _get_nested_obj_and_force
        else:
            self.get_nested_obj = _get_nested_obj
        self.reset()

    def __repr__(self):
        return "<Delta: {}>".format(short_repr(self.diff, max_length=100))

    def reset(self):
        self.post_process_paths_to_convert = dict_()

    def __add__(self, other):
        if isinstance(other, numbers) and self._numpy_paths:
            raise DeltaNumpyOperatorOverrideError(DELTA_NUMPY_OPERATOR_OVERRIDE_MSG)
        if self.mutate:
            self.root = other
        else:
            self.root = deepcopy(other)
        self._do_pre_process()
        self._do_values_changed()
        self._do_set_item_added()
        self._do_set_item_removed()
        self._do_type_changes()
        # NOTE: the remove iterable action needs to happen BEFORE
        # all the other iterables to match the reverse of order of operations in DeepDiff
        self._do_iterable_opcodes()
        self._do_iterable_item_removed()
        self._do_iterable_item_added()
        self._do_ignore_order()
        self._do_dictionary_item_added()
        self._do_dictionary_item_removed()
        self._do_attribute_added()
        self._do_attribute_removed()
        self._do_post_process()

        other = self.root
        # removing the reference to other
        del self.root
        self.reset()
        return other

    __radd__ = __add__

    def __rsub__(self, other):
        if self._reversed_diff is None:
            self._reversed_diff = self._get_reverse_diff()
        self.diff, self._reversed_diff = self._reversed_diff, self.diff
        result = self.__add__(other)
        self.diff, self._reversed_diff = self._reversed_diff, self.diff
        return result

    def _raise_or_log(self, msg, level='error'):
        if self.log_errors:
            getattr(logger, level)(msg)
        if self.raise_errors:
            raise DeltaError(msg)

    def _do_verify_changes(self, path, expected_old_value, current_old_value):
        if self.bidirectional and expected_old_value != current_old_value:
            if isinstance(path, str):
                path_str = path
            else:
                path_str = stringify_path(path, root_element=('', GETATTR))
            self._raise_or_log(VERIFICATION_MSG.format(
                path_str, expected_old_value, current_old_value, VERIFY_BIDIRECTIONAL_MSG))

    def _get_elem_and_compare_to_old_value(
        self,
        obj,
        path_for_err_reporting,
        expected_old_value,
        elem=None,
        action=None,
        forced_old_value=None,
        next_element=None,
    ):
        # if forced_old_value is not None:
        try:
            if action == GET:
                current_old_value = obj[elem]
            elif action == GETATTR:
                current_old_value = getattr(obj, elem)
            else:
                raise DeltaError(INVALID_ACTION_WHEN_CALLING_GET_ELEM.format(action))
        except (KeyError, IndexError, AttributeError, TypeError) as e:
            if self.force:
                if forced_old_value is None:
                    if next_element is None or isinstance(next_element, str):
                        _forced_old_value = {}
                    else:
                        _forced_old_value = []    
                else:
                    _forced_old_value = forced_old_value
                if action == GET:
                    if isinstance(obj, list):
                        if isinstance(elem, int) and elem < len(obj):
                            obj[elem] = _forced_old_value
                        else:
                            obj.append(_forced_old_value)
                    else:
                        obj[elem] = _forced_old_value
                elif action == GETATTR:
                    setattr(obj, elem, _forced_old_value)
                return _forced_old_value
            current_old_value = not_found
            if isinstance(path_for_err_reporting, (list, tuple)):
                path_for_err_reporting = '.'.join([i[0] for i in path_for_err_reporting])
            if self.bidirectional:
                self._raise_or_log(VERIFICATION_MSG.format(
                    path_for_err_reporting,
                    expected_old_value, current_old_value, e))
            else:
                self._raise_or_log(UNABLE_TO_GET_PATH_MSG.format(
                    path_for_err_reporting))
        return current_old_value

    def _simple_set_elem_value(self, obj, path_for_err_reporting, elem=None, value=None, action=None):
        """
        Set the element value directly on an object
        """
        try:
            if action == GET:
                try:
                    obj[elem] = value
                except IndexError:
                    if elem == len(obj):
                        obj.append(value)
                    else:
                        self._raise_or_log(ELEM_NOT_FOUND_TO_ADD_MSG.format(elem, path_for_err_reporting))
            elif action == GETATTR:
                setattr(obj, elem, value)
            else:
                raise DeltaError(INVALID_ACTION_WHEN_CALLING_SIMPLE_SET_ELEM.format(action))
        except (KeyError, IndexError, AttributeError, TypeError) as e:
            self._raise_or_log('Failed to set {} due to {}'.format(path_for_err_reporting, e))

    def _coerce_obj(self, parent, obj, path, parent_to_obj_elem,
                    parent_to_obj_action, elements, to_type, from_type):
        """
        Coerce obj and mark it in post_process_paths_to_convert for later to be converted back.
        Also reassign it to its parent to replace the old object.
        """
        self.post_process_paths_to_convert[elements[:-1]] = {'old_type': to_type, 'new_type': from_type}
        # If this function is going to ever be used to convert numpy arrays, uncomment these lines:
        # if from_type is np_ndarray:
        #     obj = obj.tolist()
        # else:
        obj = to_type(obj)

        if parent:
            # Making sure that the object is re-instated inside the parent especially if it was immutable
            # and we had to turn it into a mutable one. In such cases the object has a new id.
            self._simple_set_elem_value(obj=parent, path_for_err_reporting=path, elem=parent_to_obj_elem,
                                        value=obj, action=parent_to_obj_action)
        return obj

    def _set_new_value(self, parent, parent_to_obj_elem, parent_to_obj_action,
                       obj, elements, path, elem, action, new_value):
        """
        Set the element value on an object and if necessary convert the object to the proper mutable type
        """
        if isinstance(obj, tuple):
            # convert this object back to a tuple later
            obj = self._coerce_obj(
                parent, obj, path, parent_to_obj_elem,
                parent_to_obj_action, elements,
                to_type=list, from_type=tuple)
        if elem != 0 and self.force and isinstance(obj, list) and len(obj) == 0:
            # it must have been a dictionary    
            obj = {}
            self._simple_set_elem_value(obj=parent, path_for_err_reporting=path, elem=parent_to_obj_elem,
                                        value=obj, action=parent_to_obj_action)
        self._simple_set_elem_value(obj=obj, path_for_err_reporting=path, elem=elem,
                                    value=new_value, action=action)

    def _simple_delete_elem(self, obj, path_for_err_reporting, elem=None, action=None):
        """
        Delete the element directly on an object
        """
        try:
            if action == GET:
                del obj[elem]
            elif action == GETATTR:
                del obj.__dict__[elem]
            else:
                raise DeltaError(INVALID_ACTION_WHEN_CALLING_SIMPLE_DELETE_ELEM.format(action))
        except (KeyError, IndexError, AttributeError) as e:
            self._raise_or_log('Failed to set {} due to {}'.format(path_for_err_reporting, e))

    def _del_elem(self, parent, parent_to_obj_elem, parent_to_obj_action,
                  obj, elements, path, elem, action):
        """
        Delete the element value on an object and if necessary convert the object to the proper mutable type
        """
        obj_is_new = False
        if isinstance(obj, tuple):
            # convert this object back to a tuple later
            self.post_process_paths_to_convert[elements[:-1]] = {'old_type': list, 'new_type': tuple}
            obj = list(obj)
            obj_is_new = True
        self._simple_delete_elem(obj=obj, path_for_err_reporting=path, elem=elem, action=action)
        if obj_is_new and parent:
            # Making sure that the object is re-instated inside the parent especially if it was immutable
            # and we had to turn it into a mutable one. In such cases the object has a new id.
            self._simple_set_elem_value(obj=parent, path_for_err_reporting=path, elem=parent_to_obj_elem,
                                        value=obj, action=parent_to_obj_action)

    def _do_iterable_item_added(self):
        iterable_item_added = self.diff.get('iterable_item_added', {})
        iterable_item_moved = self.diff.get('iterable_item_moved')

        # First we need to create a placeholder for moved items.
        # This will then get replaced below after we go through added items.
        # Without this items can get double added because moved store the new_value and does not need item_added replayed
        if iterable_item_moved:
            added_dict = {v["new_path"]: None for k, v in iterable_item_moved.items()}
            iterable_item_added.update(added_dict)

        if iterable_item_added:
            self._do_item_added(iterable_item_added, insert=True)

        if iterable_item_moved:
            added_dict = {v["new_path"]: v["value"] for k, v in iterable_item_moved.items()}
            self._do_item_added(added_dict, insert=False)

    def _do_dictionary_item_added(self):
        dictionary_item_added = self.diff.get('dictionary_item_added')
        if dictionary_item_added:
            self._do_item_added(dictionary_item_added, sort=False)

    def _do_attribute_added(self):
        attribute_added = self.diff.get('attribute_added')
        if attribute_added:
            self._do_item_added(attribute_added)

    @staticmethod
    def _sort_key_for_item_added(path_and_value):
        elements = _path_to_elements(path_and_value[0])
        # Example elements: [(4.3, 'GET'), ('b', 'GETATTR'), ('a3', 'GET')]
        # We only care about the values in the elements not how to get the values.
        return [i[0] for i in elements] 

    def _do_item_added(self, items, sort=True, insert=False):
        if sort:
            # sorting items by their path so that the items with smaller index
            # are applied first (unless `sort` is `False` so that order of
            # added items is retained, e.g. for dicts).
            items = sorted(items.items(), key=self._sort_key_for_item_added)
        else:
            items = items.items()

        for path, new_value in items:
            elem_and_details = self._get_elements_and_details(path)
            if elem_and_details:
                elements, parent, parent_to_obj_elem, parent_to_obj_action, obj, elem, action = elem_and_details
            else:
                continue  # pragma: no cover. Due to cPython peephole optimizer, this line doesn't get covered. https://github.com/nedbat/coveragepy/issues/198

            # Insert is only true for iterables, make sure it is a valid index.
            if(insert and elem < len(obj)):
                obj.insert(elem, None)

            self._set_new_value(parent, parent_to_obj_elem, parent_to_obj_action,
                                obj, elements, path, elem, action, new_value)

    def _do_values_changed(self):
        values_changed = self.diff.get('values_changed')
        if values_changed:
            self._do_values_or_type_changed(values_changed)

    def _do_type_changes(self):
        type_changes = self.diff.get('type_changes')
        if type_changes:
            self._do_values_or_type_changed(type_changes, is_type_change=True)

    def _do_post_process(self):
        if self.post_process_paths_to_convert:
            # Example: We had converted some object to be mutable and now we are converting them back to be immutable.
            # We don't need to check the change because it is not really a change that was part of the original diff.
            self._do_values_or_type_changed(self.post_process_paths_to_convert, is_type_change=True, verify_changes=False)

    def _do_pre_process(self):
        if self._numpy_paths and ('iterable_item_added' in self.diff or 'iterable_item_removed' in self.diff):
            preprocess_paths = dict_()
            for path, type_ in self._numpy_paths.items():
                preprocess_paths[path] = {'old_type': np_ndarray, 'new_type': list}
                try:
                    type_ = numpy_dtype_string_to_type(type_)
                except Exception as e:
                    self._raise_or_log(NOT_VALID_NUMPY_TYPE.format(e))
                    continue  # pragma: no cover. Due to cPython peephole optimizer, this line doesn't get covered. https://github.com/nedbat/coveragepy/issues/198
                self.post_process_paths_to_convert[path] = {'old_type': list, 'new_type': type_}
            if preprocess_paths:
                self._do_values_or_type_changed(preprocess_paths, is_type_change=True)

    def _get_elements_and_details(self, path):
        try:
            elements = _path_to_elements(path)
            if len(elements) > 1:
                elements_subset = elements[:-2]
                if len(elements_subset) != len(elements):
                    next_element = elements[-2][0]
                    next2_element = elements[-1][0]
                else:
                    next_element = None
                parent = self.get_nested_obj(obj=self, elements=elements_subset, next_element=next_element)
                parent_to_obj_elem, parent_to_obj_action = elements[-2]
                obj = self._get_elem_and_compare_to_old_value(
                    obj=parent, path_for_err_reporting=path, expected_old_value=None,
                    elem=parent_to_obj_elem, action=parent_to_obj_action, next_element=next2_element)
            else:
                # parent = self
                # obj = self.root
                # parent_to_obj_elem = 'root'
                # parent_to_obj_action = GETATTR
                parent = parent_to_obj_elem = parent_to_obj_action = None
                obj = self
                # obj = self.get_nested_obj(obj=self, elements=elements[:-1])
            elem, action = elements[-1]
        except Exception as e:
            self._raise_or_log(UNABLE_TO_GET_ITEM_MSG.format(path, e))
            return None
        else:
            if obj is not_found:
                return None
            return elements, parent, parent_to_obj_elem, parent_to_obj_action, obj, elem, action

    def _do_values_or_type_changed(self, changes, is_type_change=False, verify_changes=True):
        for path, value in changes.items():
            elem_and_details = self._get_elements_and_details(path)
            if elem_and_details:
                elements, parent, parent_to_obj_elem, parent_to_obj_action, obj, elem, action = elem_and_details
            else:
                continue  # pragma: no cover. Due to cPython peephole optimizer, this line doesn't get covered. https://github.com/nedbat/coveragepy/issues/198
            expected_old_value = value.get('old_value', not_found)

            current_old_value = self._get_elem_and_compare_to_old_value(
                obj=obj, path_for_err_reporting=path, expected_old_value=expected_old_value, elem=elem, action=action)
            if current_old_value is not_found:
                continue  # pragma: no cover. I have not been able to write a test for this case. But we should still check for it.
            # With type change if we could have originally converted the type from old_value
            # to new_value just by applying the class of the new_value, then we might not include the new_value
            # in the delta dictionary. That is defined in Model.DeltaResult._from_tree_type_changes
            if is_type_change and 'new_value' not in value:
                try:
                    new_type = value['new_type']
                    # in case of Numpy we pass the ndarray plus the dtype in a tuple
                    if new_type in numpy_dtypes:
                        new_value = np_array_factory(current_old_value, new_type)
                    else:
                        new_value = new_type(current_old_value)
                except Exception as e:
                    self._raise_or_log(TYPE_CHANGE_FAIL_MSG.format(obj[elem], value.get('new_type', 'unknown'), e))
                    continue
            else:
                new_value = value['new_value']

            self._set_new_value(parent, parent_to_obj_elem, parent_to_obj_action,
                                obj, elements, path, elem, action, new_value)

            if verify_changes:
                self._do_verify_changes(path, expected_old_value, current_old_value)

    def _do_item_removed(self, items):
        """
        Handle removing items.
        """
        # Sorting the iterable_item_removed in reverse order based on the paths.
        # So that we delete a bigger index before a smaller index
        for path, expected_old_value in sorted(items.items(), key=self._sort_key_for_item_added, reverse=True):
            elem_and_details = self._get_elements_and_details(path)
            if elem_and_details:
                elements, parent, parent_to_obj_elem, parent_to_obj_action, obj, elem, action = elem_and_details
            else:
                continue  # pragma: no cover. Due to cPython peephole optimizer, this line doesn't get covered. https://github.com/nedbat/coveragepy/issues/198

            look_for_expected_old_value = False
            current_old_value = not_found
            try:
                if action == GET:
                    current_old_value = obj[elem]
                elif action == GETATTR:
                    current_old_value = getattr(obj, elem)
                look_for_expected_old_value = current_old_value != expected_old_value
            except (KeyError, IndexError, AttributeError, TypeError):
                look_for_expected_old_value = True

            if look_for_expected_old_value and isinstance(obj, list) and not self._iterable_compare_func_was_used:
                # It may return None if it doesn't find it
                elem = self._find_closest_iterable_element_for_index(obj, elem, expected_old_value)
                if elem is not None:
                    current_old_value = expected_old_value
            if current_old_value is not_found or elem is None:
                continue

            self._del_elem(parent, parent_to_obj_elem, parent_to_obj_action,
                           obj, elements, path, elem, action)
            self._do_verify_changes(path, expected_old_value, current_old_value)

    def _find_closest_iterable_element_for_index(self, obj, elem, expected_old_value):
        closest_elem = None
        closest_distance = float('inf')
        for index, value in enumerate(obj):
            dist = abs(index - elem)
            if dist > closest_distance:
                break
            if value == expected_old_value and dist < closest_distance:
                closest_elem = index
                closest_distance = dist
        return closest_elem

    def _do_iterable_opcodes(self):
        _iterable_opcodes = self.diff.get('_iterable_opcodes', {})
        if _iterable_opcodes:
            for path, opcodes in _iterable_opcodes.items():
                transformed = []
                # elements = _path_to_elements(path)
                elem_and_details = self._get_elements_and_details(path)
                if elem_and_details:
                    elements, parent, parent_to_obj_elem, parent_to_obj_action, obj, elem, action = elem_and_details
                    if parent is None:
                        parent = self
                        obj = self.root
                        parent_to_obj_elem = 'root'
                        parent_to_obj_action = GETATTR
                else:
                    continue  # pragma: no cover. Due to cPython peephole optimizer, this line doesn't get covered. https://github.com/nedbat/coveragepy/issues/198
                # import pytest; pytest.set_trace()
                obj = self.get_nested_obj(obj=self, elements=elements)
                is_obj_tuple = isinstance(obj, tuple)
                for opcode in opcodes:    
                    if opcode.tag == 'replace':
                        # Replace items in list a[i1:i2] with b[j1:j2]
                        transformed.extend(opcode.new_values)
                    elif opcode.tag == 'delete':
                        # Delete items from list a[i1:i2], so we do nothing here
                        continue
                    elif opcode.tag == 'insert':
                        # Insert items from list b[j1:j2] into the new list
                        transformed.extend(opcode.new_values)
                    elif opcode.tag == 'equal':
                        # Items are the same in both lists, so we add them to the result
                        transformed.extend(obj[opcode.t1_from_index:opcode.t1_to_index])
                if is_obj_tuple:
                    obj = tuple(obj)
                    # Making sure that the object is re-instated inside the parent especially if it was immutable
                    # and we had to turn it into a mutable one. In such cases the object has a new id.
                    self._simple_set_elem_value(obj=parent, path_for_err_reporting=path, elem=parent_to_obj_elem,
                                                value=obj, action=parent_to_obj_action)
                else:
                    obj[:] = transformed



                # obj = self.get_nested_obj(obj=self, elements=elements)
                # for


    def _do_iterable_item_removed(self):
        iterable_item_removed = self.diff.get('iterable_item_removed', {})

        iterable_item_moved = self.diff.get('iterable_item_moved')
        if iterable_item_moved:
            # These will get added back during items_added
            removed_dict = {k: v["value"] for k, v in iterable_item_moved.items()}
            iterable_item_removed.update(removed_dict)

        if iterable_item_removed:
            self._do_item_removed(iterable_item_removed)

    def _do_dictionary_item_removed(self):
        dictionary_item_removed = self.diff.get('dictionary_item_removed')
        if dictionary_item_removed:
            self._do_item_removed(dictionary_item_removed)

    def _do_attribute_removed(self):
        attribute_removed = self.diff.get('attribute_removed')
        if attribute_removed:
            self._do_item_removed(attribute_removed)

    def _do_set_item_added(self):
        items = self.diff.get('set_item_added')
        if items:
            self._do_set_or_frozenset_item(items, func='union')

    def _do_set_item_removed(self):
        items = self.diff.get('set_item_removed')
        if items:
            self._do_set_or_frozenset_item(items, func='difference')

    def _do_set_or_frozenset_item(self, items, func):
        for path, value in items.items():
            elements = _path_to_elements(path)
            parent = self.get_nested_obj(obj=self, elements=elements[:-1])
            elem, action = elements[-1]
            obj = self._get_elem_and_compare_to_old_value(
                parent, path_for_err_reporting=path, expected_old_value=None, elem=elem, action=action, forced_old_value=set())
            new_value = getattr(obj, func)(value)
            self._simple_set_elem_value(parent, path_for_err_reporting=path, elem=elem, value=new_value, action=action)

    def _do_ignore_order_get_old(self, obj, remove_indexes_per_path, fixed_indexes_values, path_for_err_reporting):
        """
        A generator that gets the old values in an iterable when the order was supposed to be ignored.
        """
        old_obj_index = -1
        max_len = len(obj) - 1
        while old_obj_index < max_len:
            old_obj_index += 1
            current_old_obj = obj[old_obj_index]
            if current_old_obj in fixed_indexes_values:
                continue
            if old_obj_index in remove_indexes_per_path:
                expected_obj_to_delete = remove_indexes_per_path.pop(old_obj_index)
                if current_old_obj == expected_obj_to_delete:
                    continue
                else:
                    self._raise_or_log(FAIL_TO_REMOVE_ITEM_IGNORE_ORDER_MSG.format(
                        old_obj_index, path_for_err_reporting, expected_obj_to_delete, current_old_obj))
            yield current_old_obj

    def _do_ignore_order(self):
        """

            't1': [5, 1, 1, 1, 6],
            't2': [7, 1, 1, 1, 8],

            'iterable_items_added_at_indexes': {
                'root': {
                    0: 7,
                    4: 8
                }
            },
            'iterable_items_removed_at_indexes': {
                'root': {
                    4: 6,
                    0: 5
                }
            }

        """
        fixed_indexes = self.diff.get('iterable_items_added_at_indexes', dict_())
        remove_indexes = self.diff.get('iterable_items_removed_at_indexes', dict_())
        paths = OrderedSet(fixed_indexes.keys()) | OrderedSet(remove_indexes.keys())
        for path in paths:
            # In the case of ignore_order reports, we are pointing to the container object.
            # Thus we add a [0] to the elements so we can get the required objects and discard what we don't need.
            elem_and_details = self._get_elements_and_details("{}[0]".format(path))
            if elem_and_details:
                _, parent, parent_to_obj_elem, parent_to_obj_action, obj, _, _ = elem_and_details
            else:
                continue  # pragma: no cover. Due to cPython peephole optimizer, this line doesn't get covered. https://github.com/nedbat/coveragepy/issues/198
            # copying both these dictionaries since we don't want to mutate them.
            fixed_indexes_per_path = fixed_indexes.get(path, dict_()).copy()
            remove_indexes_per_path = remove_indexes.get(path, dict_()).copy()
            fixed_indexes_values = AnySet(fixed_indexes_per_path.values())

            new_obj = []
            # Numpy's NdArray does not like the bool function.
            if isinstance(obj, np_ndarray):
                there_are_old_items = obj.size > 0
            else:
                there_are_old_items = bool(obj)
            old_item_gen = self._do_ignore_order_get_old(
                obj, remove_indexes_per_path, fixed_indexes_values, path_for_err_reporting=path)
            while there_are_old_items or fixed_indexes_per_path:
                new_obj_index = len(new_obj)
                if new_obj_index in fixed_indexes_per_path:
                    new_item = fixed_indexes_per_path.pop(new_obj_index)
                    new_obj.append(new_item)
                elif there_are_old_items:
                    try:
                        new_item = next(old_item_gen)
                    except StopIteration:
                        there_are_old_items = False
                    else:
                        new_obj.append(new_item)
                else:
                    # pop a random item from the fixed_indexes_per_path dictionary
                    self._raise_or_log(INDEXES_NOT_FOUND_WHEN_IGNORE_ORDER.format(fixed_indexes_per_path))
                    new_item = fixed_indexes_per_path.pop(next(iter(fixed_indexes_per_path)))
                    new_obj.append(new_item)

            if isinstance(obj, tuple):
                new_obj = tuple(new_obj)
            # Making sure that the object is re-instated inside the parent especially if it was immutable
            # and we had to turn it into a mutable one. In such cases the object has a new id.
            self._simple_set_elem_value(obj=parent, path_for_err_reporting=path, elem=parent_to_obj_elem,
                                        value=new_obj, action=parent_to_obj_action)

    def _get_reverse_diff(self):
        if not self.bidirectional:
            raise ValueError('Please recreate the delta with bidirectional=True')

        SIMPLE_ACTION_TO_REVERSE = {
            'iterable_item_added': 'iterable_item_removed',
            'iterable_items_added_at_indexes': 'iterable_items_removed_at_indexes',
            'attribute_added': 'attribute_removed',
            'set_item_added': 'set_item_removed',
            'dictionary_item_added': 'dictionary_item_removed',
        }
        # Adding the reverse of the dictionary
        for key in list(SIMPLE_ACTION_TO_REVERSE.keys()):
            SIMPLE_ACTION_TO_REVERSE[SIMPLE_ACTION_TO_REVERSE[key]] = key

        r_diff = {}
        for action, info in self.diff.items():
            reverse_action = SIMPLE_ACTION_TO_REVERSE.get(action)
            if reverse_action:
                r_diff[reverse_action] = info
            elif action == 'values_changed':
                r_diff[action] = {}
                for path, path_info in info.items():
                    r_diff[action][path] = {
                        'new_value': path_info['old_value'], 'old_value': path_info['new_value']
                    } 
            elif action == 'type_changes':
                r_diff[action] = {}
                for path, path_info in info.items():
                    r_diff[action][path] = {
                        'old_type': path_info['new_type'], 'new_type': path_info['old_type'],
                    }
                    if 'new_value' in path_info:
                        r_diff[action][path]['old_value'] = path_info['new_value']
                    if 'old_value' in path_info:
                        r_diff[action][path]['new_value'] = path_info['old_value']
            elif action == 'iterable_item_moved':
                r_diff[action] = {}
                for path, path_info in info.items():
                    old_path = path_info['new_path']
                    r_diff[action][old_path] = {
                        'new_path': path, 'value': path_info['value'],
                    }
            elif action == '_iterable_opcodes':
                r_diff[action] = {}
                for path, op_codes in info.items():
                    r_diff[action][path] = []
                    for op_code in op_codes:
                        tag = op_code.tag
                        tag = {'delete': 'insert', 'insert': 'delete'}.get(tag, tag)
                        new_op_code = Opcode(
                            tag=tag,
                            t1_from_index=op_code.t2_from_index,
                            t1_to_index=op_code.t2_to_index,
                            t2_from_index=op_code.t1_from_index,
                            t2_to_index=op_code.t1_to_index,
                            new_values=op_code.old_values,
                            old_values=op_code.new_values,
                        )
                        r_diff[action][path].append(new_op_code)
        return r_diff

    def dump(self, file):
        """
        Dump into file object
        """
        # Small optimization: Our internal pickle serializer can just take a file object
        # and directly write to it. However if a user defined serializer is passed
        # we want to make it compatible with the expectation that self.serializer(self.diff)
        # will give the user the serialization and then it can be written to
        # a file object when using the dump(file) function.
        param_names_of_serializer = set(self.serializer.__code__.co_varnames)
        if 'file_obj' in param_names_of_serializer:
            self.serializer(self.diff, file_obj=file)
        else:
            file.write(self.dumps())

    def dumps(self):
        """
        Return the serialized representation of the object as a bytes object, instead of writing it to a file.
        """
        return self.serializer(self.diff)

    def to_dict(self):
        return dict(self.diff)

    @staticmethod
    def _get_flat_row(action, info, _parse_path, keys_and_funcs):
        for path, details in info.items():
            row = {'path': _parse_path(path), 'action': action}
            for key, new_key, func in keys_and_funcs:
                if key in details:
                    if func:
                        row[new_key] = func(details[key])
                    else:
                        row[new_key] = details[key]
            yield FlatDeltaRow(**row)

    @staticmethod
    def _from_flat_rows(flat_rows_list: List[FlatDeltaRow]):
        flat_dict_list = (i._asdict() for i in flat_rows_list)
        return Delta._from_flat_dicts(flat_dict_list)

    @staticmethod
    def _from_flat_dicts(flat_dict_list):
        """
        Create the delta's diff object from the flat_dict_list
        """
        result = {}
        FLATTENING_NEW_ACTION_MAP = {
            'unordered_iterable_item_added': 'iterable_items_added_at_indexes',
            'unordered_iterable_item_removed': 'iterable_items_removed_at_indexes',
        }
        for flat_dict in flat_dict_list:
            index = None
            action = flat_dict.get("action")
            path = flat_dict.get("path")
            value = flat_dict.get('value')
            old_value = flat_dict.get('old_value', UnkownValueCode)
            if not action:
                raise ValueError("Flat dict need to include the 'action'.")
            if path is None:
                raise ValueError("Flat dict need to include the 'path'.")
            if action in FLATTENING_NEW_ACTION_MAP:
                action = FLATTENING_NEW_ACTION_MAP[action]
                index = path.pop()
            if action in {'attribute_added', 'attribute_removed'}:
                root_element = ('root', GETATTR)
            else:
                root_element = ('root', GET)
            path_str = stringify_path(path, root_element=root_element)  # We need the string path
            if action not in result:
                result[action] = {}
            if action in {'iterable_items_added_at_indexes', 'iterable_items_removed_at_indexes'}:
                if path_str not in result[action]:
                    result[action][path_str] = {}
                result[action][path_str][index] = value
            elif action in {'set_item_added', 'set_item_removed'}:
                if path_str not in result[action]:
                    result[action][path_str] = set()
                result[action][path_str].add(value)
            elif action in {
                'dictionary_item_added', 'dictionary_item_removed',
                'attribute_removed', 'attribute_added', 'iterable_item_added', 'iterable_item_removed',
            }:
                result[action][path_str] = value
            elif action == 'values_changed':
                if old_value == UnkownValueCode:
                    result[action][path_str] = {'new_value': value}
                else:
                    result[action][path_str] = {'new_value': value, 'old_value': old_value}
            elif action == 'type_changes':
                type_ = flat_dict.get('type', UnkownValueCode)
                old_type = flat_dict.get('old_type', UnkownValueCode)

                result[action][path_str] = {'new_value': value}
                for elem, elem_value in [
                    ('new_type', type_),
                    ('old_type', old_type),
                    ('old_value', old_value),
                ]:
                    if elem_value != UnkownValueCode:
                        result[action][path_str][elem] = elem_value
            elif action == 'iterable_item_moved':
                result[action][path_str] = {
                    'new_path': stringify_path(
                        flat_dict.get('new_path', ''),
                        root_element=('root', GET)
                    ),
                    'value': value,
                }

        return result

    def _flatten_iterable_opcodes(self):
        result = []
        for path, opcodes in self.diff['_iterable_opcodes']:
            for opcode in opcodes:
                if opcode.tag == '':
                    pass

    def to_flat_dicts(self, include_action_in_path=False, report_type_changes=True) -> List[FlatDeltaRow]:
        """
        Returns a flat list of actions that is easily machine readable.

        For example:
            {'iterable_item_added': {'root[3]': 5, 'root[2]': 3}}

        Becomes:
            [
                {'path': [3], 'value': 5, 'action': 'iterable_item_added'},
                {'path': [2], 'value': 3, 'action': 'iterable_item_added'},
            ]

        
        **Parameters**

        include_action_in_path : Boolean, default=False
            When False, we translate DeepDiff's paths like root[3].attribute1 into a [3, 'attribute1'].
            When True, we include the action to retrieve the item in the path: [(3, 'GET'), ('attribute1', 'GETATTR')]
            Note that the "action" here is the different than the action reported by to_flat_dicts. The action here is just about the "path" output.

        report_type_changes : Boolean, default=True
            If False, we don't report the type change. Instead we report the value change.

        Example:
            t1 = {"a": None}
            t2 = {"a": 1}

            dump = Delta(DeepDiff(t1, t2)).dumps()
            delta = Delta(dump)
            assert t2 == delta + t1

            flat_result = delta.to_flat_dicts()
            flat_expected = [{'path': ['a'], 'action': 'type_changes', 'value': 1, 'new_type': int, 'old_type': type(None)}]
            assert flat_expected == flat_result

            flat_result2 = delta.to_flat_dicts(report_type_changes=False)
            flat_expected2 = [{'path': ['a'], 'action': 'values_changed', 'value': 1}]

        **List of actions**

        Here are the list of actions that the flat dictionary can return.
            iterable_item_added
            iterable_item_removed
            iterable_item_moved
            values_changed
            type_changes
            set_item_added
            set_item_removed
            dictionary_item_added
            dictionary_item_removed
            attribute_added
            attribute_removed
        """
        return [
            i._asdict() for i in self.to_flat_rows(include_action_in_path=False, report_type_changes=True)
        ]

    def to_flat_rows(self, include_action_in_path=False, report_type_changes=True) -> List[FlatDeltaRow]:
        """
        Just like to_flat_dicts but returns FlatDeltaRow Named Tuples
        """
        result = []
        if include_action_in_path:
            _parse_path = partial(parse_path, include_actions=True)
        else:
            _parse_path = parse_path
        if report_type_changes:
            keys_and_funcs = [
                ('value', 'value', None),
                ('new_value', 'value', None),
                ('old_value', 'old_value', None),
                ('new_type', 'type', None),
                ('old_type', 'old_type', None),
                ('new_path', 'new_path', _parse_path),
            ]
        else:
            if not self.always_include_values:
                raise ValueError(
                    "When converting to flat dictionaries, if report_type_changes=False and there are type changes, "
                    "you must set the always_include_values=True at the delta object creation. Otherwise there is nothing to include."
                )
            keys_and_funcs = [
                ('value', 'value', None),
                ('new_value', 'value', None),
                ('old_value', 'old_value', None),
                ('new_path', 'new_path', _parse_path),
            ]

        FLATTENING_NEW_ACTION_MAP = {
            'iterable_items_added_at_indexes': 'unordered_iterable_item_added',
            'iterable_items_removed_at_indexes': 'unordered_iterable_item_removed',
        }
        for action, info in self.diff.items():
            if action.startswith('_'):
                continue
            if action in FLATTENING_NEW_ACTION_MAP:
                new_action = FLATTENING_NEW_ACTION_MAP[action]
                for path, index_to_value in info.items():
                    path = _parse_path(path)
                    for index, value in index_to_value.items():
                        path2 = path.copy()
                        if include_action_in_path:
                            path2.append((index, 'GET'))
                        else:
                            path2.append(index)
                        result.append(FlatDeltaRow(path=path2, value=value, action=new_action))
            elif action in {'set_item_added', 'set_item_removed'}:
                for path, values in info.items():
                    path = _parse_path(path)
                    for value in values:
                        result.append(FlatDeltaRow(path=path, value=value, action=action))
            elif action == 'dictionary_item_added':
                for path, value in info.items():
                    path = _parse_path(path)
                    if isinstance(value, dict) and len(value) == 1:
                        new_key = next(iter(value))
                        path.append(new_key)
                        value = value[new_key]
                    elif isinstance(value, (list, tuple)) and len(value) == 1:
                        value = value[0]
                        path.append(0)
                        action = 'iterable_item_added'
                    elif isinstance(value, set) and len(value) == 1:
                        value = value.pop()
                        action = 'set_item_added'
                    result.append(FlatDeltaRow(path=path, value=value, action=action))
            elif action in {
                'dictionary_item_removed', 'iterable_item_added',
                'iterable_item_removed', 'attribute_removed', 'attribute_added'
            }:
                for path, value in info.items():
                    path = _parse_path(path)
                    result.append(FlatDeltaRow(path=path, value=value, action=action))
            elif action == 'type_changes':
                if not report_type_changes:
                    action = 'values_changed'

                for row in self._get_flat_row(
                    action=action,
                    info=info,
                    _parse_path=_parse_path,
                    keys_and_funcs=keys_and_funcs,
                ):
                    result.append(row)
            elif action == '_iterable_opcodes':
                result.extend(self._flatten_iterable_opcodes())
            else:
                for row in self._get_flat_row(
                    action=action,
                    info=info,
                    _parse_path=_parse_path,
                    keys_and_funcs=keys_and_funcs,
                ):
                    result.append(row)
        return result


if __name__ == "__main__":  # pragma: no cover
    import doctest
    doctest.testmod()
