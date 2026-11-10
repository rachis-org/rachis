# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import pathlib
from typing import get_origin, get_args, Union

from rachis import sdk
from rachis.plugin import model
from rachis.core import util
from rachis.core.exceptions import ValidationError


def identity_transformer(view):
    return view


class ModelType:
    @staticmethod
    def from_view_type(view_type):
        if get_origin(view_type) is Union:
            return UnionType(view_type)
        elif issubclass(view_type, model.base.FormatBase):
            if issubclass(view_type,
                          model.SingleFileDirectoryFormatBase):
                # HACK: this is necessary because we need to be able to "act"
                # like a FileFormat when looking up transformers, but our
                # input/output coercion still needs to bridge the
                # transformation as we do not have transitivity

                # In other words we have DX and we have transformers of X
                # In a perfect world we would automatically define DX -> X and
                # let transitivity handle it, but since that doesn't exist, we
                # need to treat DX as if it were X and coerce behind the scenes

                # TODO: redo this when transformers are transitive
                return SingleFileDirectoryFormatType(view_type)
            # Normal format type
            return FormatType(view_type)
        else:
            # TODO: supporting stdlib.typing may require an alternate
            # model type as `isinstance` is a meaningless operation
            # for them so validation would need to be handled differently
            return ObjectType(view_type)

    def __init__(self, view_type):
        self._pm = sdk.PluginManager()
        self._view_type = view_type
        self._view_name = util.get_view_name(self._view_type)
        self._record = None

        if self._view_name in self._pm.views:
            self._record = self._pm.views[self._view_name]

    def make_transformation(self, other, recorder=None):
        # record may be None in case of identity transformer
        transformer, transformer_record = self._get_transformer_to(other)
        if transformer is None:
            raise Exception("No transformation from %r to %r" %
                            (self._view_type, other._view_type))

        if recorder is not None:
            recorder(transformer_record, input_name=self._view_name,
                     input_record=self._record, output_name=other._view_name,
                     output_record=other._record)

        def transformation(view, validate_level='min'):
            view = self.coerce_view(view)
            self.validate(view, level=validate_level)

            new_view = transformer(view)

            new_view = other.coerce_view(new_view)
            other.validate(new_view)

            if transformer is not identity_transformer:
                other.set_user_owned(new_view, False)

            return new_view

        return transformation

    def _get_transformer_to(self, other):
        transformer, record = self._lookup_transformer(self._view_type,
                                                       other._view_type)
        if transformer is None:
            return other._get_transformer_from(self)

        return transformer, record

    def has_transformation(self, other):
        """ Checks to see if there exist transformers for other

        Parameters
        ----------
        other : ModelType subclass
           The object being checked for transformer

        Returns
        -------
        bool
            Does the specified transformer exist for other?
        """

        transformer, _ = self._get_transformer_to(other)
        return transformer is not None

    def _get_transformer_from(self, other):
        return None, None

    def coerce_view(self, view):
        return view

    def _lookup_transformer(self, from_, to_):
        if from_ == to_:
            return identity_transformer, None
        try:
            record = self._pm.transformers[from_][to_]
            return record.transformer, record
        except KeyError:
            return None, None

    def set_user_owned(self, view, value):
        pass


class FormatType(ModelType):
    def coerce_view(self, view):
        if type(view) is str or isinstance(view, pathlib.Path):
            return self._view_type(view, mode='r')

        if isinstance(view, self._view_type):
            # wrap original path (inheriting the lifetime) and return a
            # read-only instance
            return self._view_type(view.path, mode='r')

        return view

    def validate(self, view, level='min'):
        if not isinstance(view, self._view_type):
            raise ValidationError("%r is not an instance of %r."
                            % (view, self._view_type))
        # Formats have a validate method, so defer to it
        view.validate(level)

    def set_user_owned(self, view, value):
        view.path._user_owned = value


class SingleFileDirectoryFormatType(FormatType):
    def __init__(self, view_type):
        # Single file directory formats have only one file named `file`
        # allowing us construct a model type from the format of `file`
        self._wrapped_view_type = view_type.file.format
        super().__init__(view_type)

    def _get_transformer_to(self, other):
        # Legend:
        # - Dx: single directory format of x
        # - Dy: single directory format of y
        # - x: input format x
        # - y: output format y
        # - ->: implicit transformer
        # - =>: registered transformer
        # - :> final transformation
        # - |: or, used when multiple situation are possible

        # It looks like all permutations because it is...

        # Dx :> y | Dy via Dx => y | Dy
        transformer, record = self._wrap_transformer(self, other)
        if transformer is not None:
            return transformer, record

        # Dx :> Dy via Dx -> x => y | Dy
        transformer, record = self._wrap_transformer(self, other,
                                                     wrap_input=True)
        if transformer is not None:
            return transformer, record

        if type(other) is type(self):
            # Dx :> Dy via Dx -> x => y -> Dy
            transformer, record = self._wrap_transformer(
                self, other, wrap_input=True, wrap_output=True)
            if transformer is not None:
                return transformer, record

        # Out of options, try for Dx :> Dy via Dx => y -> Dy
        return other._get_transformer_from(self)  # record is included

    def _get_transformer_from(self, other):
        # x | Dx :> Dy via x | Dx => y -> Dy
        # IMPORTANT: reverse other and self, this method is like __radd__
        return self._wrap_transformer(other, self, wrap_output=True)

    def _wrap_transformer(self, in_, out_, wrap_input=False,
                          wrap_output=False):
        input = in_._wrapped_view_type if wrap_input else in_._view_type
        output = out_._wrapped_view_type if wrap_output else out_._view_type

        transformer, record = self._lookup_transformer(input, output)
        if transformer is None:
            return None, None

        if wrap_input:
            transformer = in_._wrap_input(transformer)

        if wrap_output:
            transformer = out_._wrap_output(transformer)

        return transformer, record

    def _wrap_input(self, transformer):
        def wrapped(view):
            return transformer(view.file.view(self._wrapped_view_type))

        return wrapped

    def _wrap_output(self, transformer):
        def wrapped(view):
            new_view = self._view_type()
            file_view = transformer(view)
            if transformer is not identity_transformer:
                self.set_user_owned(file_view, False)
            new_view.file.write_data(file_view, self._wrapped_view_type)
            return new_view

        return wrapped


class ObjectType(ModelType):
    def validate(self, view, level=None):
        if not isinstance(view, self._view_type):
            raise ValidationError(
                "%r is not of type %r, cannot transform further."
                % (view, self._view_type)
            )


class UnionType(ModelType):
    @classmethod
    def _is_union_type(cls, type):
        return get_origin(type) is Union

    def make_transformation(self, other, recorder=None):
        transformations = []
        if (
            self._is_union_type(self._view_type)
            and self._is_union_type(other._view_type)
        ):
            from_types = ', '.join(
                [str(arg) for arg in get_args(self._view_type)]
            )
            to_types = ', '.join(
                [str(arg) for arg in get_args(other._view_type)]
            )
            message = (
                f'No transformation from any of {from_types} to any of '
                f'{to_types}'
            )
            for from_arg in get_args(self._view_type):
                for to_arg in get_args(self._view_type):
                    from_type = ModelType.from_view_type(from_arg)
                    to_type = ModelType.from_view_type(to_arg)
                    try:
                         transformations.append(
                             from_type.make_transformation(to_type, recorder)
                         )
                    except Exception:
                        pass
        elif self._is_union_type(self._view_type):
            from_types = ', '.join(
                [str(arg) for arg in get_args(self._view_type)]
            )
            message = (
                f'No transformation from any of {from_types} to '
                f'{other._view_type}'
            )
            for arg in get_args(self._view_type):
                type = ModelType.from_view_type(arg)
                try:
                    transformations.append(
                        type.make_transformation(other, recorder)
                    )
                except Exception:
                    pass
        else:
            to_types =  ', '.join(
                [str(arg) for arg in get_args(other._view_type)]
            )
            message = f'No transformation from {self._view_type} to {to_types}'
            type = ModelType.from_view_type(self._view_type)
            for arg in get_args(other._view_type):
                arg = ModelType.from_view_type(arg)
                try:
                     transformations.append(
                         type.make_transformation(arg, recorder)
                     )
                except Exception as e:
                    print(e)
                    pass

        if len(transformations) > 0:
            return transformations

        raise Exception(message)
