from typing import NamedTuple, Optional, Sequence

import numpy
import torch

__all__ = ["_NumpyTensor"]


_INTERMEDIATE_MAPPING = {
    1: torch.int8,
    2: torch.int16,
    4: torch.int32,
    8: torch.int64,
}

# torch types with no numpy equivalents
# i.e. the only ones that need to be opaque
# Uses a comprehension to filter out any dtypes
# that don't exist in older torch versions
_ASYMMETRIC_TYPES = {
    getattr(torch, t)
    for t in (
        "bfloat16",
        "quint8",
        "qint8",
        "qint32",
        "quint4x2",
        "quint2x4",
        "complex32",
    )
    if hasattr(torch, t)
}

# These types aren't supported yet because they require supplemental
# quantization parameters to deserialize correctly
_UNSUPPORTED_TYPES = {
    getattr(torch, t)
    for t in (
        "quint8",
        "qint8",
        "qint32",
        "quint4x2",
        "quint2x4",
    )
    if hasattr(torch, t)
}

_DECODE_MAPPING = {str(t): t for t in _ASYMMETRIC_TYPES}


class _NumpyTensor(NamedTuple):
    data: numpy.ndarray
    numpy_dtype: str
    torch_dtype: Optional[str]

    @classmethod
    def from_buffer(
        cls,
        numpy_dtype: str,
        torch_dtype: Optional[str],
        shape_list: Sequence,
        buffer: memoryview,
        offset: int = 0,
    ) -> "_NumpyTensor":
        """
        Decodes a raw byte buffer into a `_NumpyTensor` given its numpy dtype,
        its torch dtype, and its shape.

        Args:
            numpy_dtype: The encoded numpy dtype of the buffer.
            torch_dtype: The encoded torch dtype of the buffer.
            shape_list: The dimensions of the array represented by the buffer.
            buffer: The raw byte buffer containing encoded array data,
                as a memoryview.
            offset: An optional offset into the buffer to start from.

        Returns:
            A `_NumpyTensor` object that can have `.to_tensor()` called on it
            to receive a torch.Tensor.
        """
        data = numpy.ndarray.__new__(
            numpy.memmap,
            shape_list,
            dtype=cls._decoder_dtype(numpy_dtype),
            buffer=buffer,
            offset=offset,
        )
        return cls(data=data, numpy_dtype=numpy_dtype, torch_dtype=torch_dtype)

    @classmethod
    def from_tensor(cls, tensor: torch.Tensor) -> "_NumpyTensor":
        """
        Converts a torch tensor into a `_NumpyTensor`.
        May use an opaque dtype for the numpy array stored in
        the ``data`` field if the tensor's torch dtype has no numpy equivalent.
        See also: `_NumpyTensor.is_opaque`.

        Args:
            tensor: A torch tensor to convert to a `_NumpyTensor`.

        Returns:
            A `_NumpyTensor` with a `data` field holding a numpy array,
            and `numpy_dtype` and torch_dtype` fields suitable for
            record-keeping for serialization and deserialization.
        """
        if tensor.dtype in _UNSUPPORTED_TYPES:
            raise NotImplementedError(
                f"Serialization for {tensor.dtype} is not implemented."
            )
        torch_dtype = str(tensor.dtype)
        tensor = tensor.cpu().detach()

        if not cls._is_asymmetric(tensor.dtype):
            try:
                arr = tensor.numpy()
                numpy_dtype = arr.dtype.str
                return cls(
                    data=arr, numpy_dtype=numpy_dtype, torch_dtype=torch_dtype
                )
            except TypeError:
                # Not a known asymmetric type, but torch can't convert it
                # so fall back to storing it as opaque data
                pass

        # Replace the dtype with some variety of int and mark as opaque data
        size = tensor.element_size()
        arr = tensor.view(cls._intermediate_type(size)).numpy()
        numpy_dtype = arr.dtype.str.replace("i", "V")
        return cls(data=arr, numpy_dtype=numpy_dtype, torch_dtype=torch_dtype)

    @classmethod
    def from_array(cls, arr: numpy.ndarray) -> "_NumpyTensor":
        """
        Converts a numpy array into a `_NumpyTensor`.
        This leaves the data as-is, but finds correct values for
        `numpy_dtype` and `torch_dtype`.

        Args:
            arr: A numpy array to convert to a `_NumpyTensor`.

        Returns:
            A `_NumpyTensor` with `arr` as its `data` field,
            and `numpy_dtype` and torch_dtype` fields suitable for
            record-keeping for serialization and deserialization.
        """
        try:
            test_array = numpy.empty((), dtype=arr.dtype)
            torch_dtype = torch.from_numpy(test_array).dtype
        except TypeError as e:
            # If something were serialized with this type,
            # it wouldn't be able to be deserialized later.
            raise TypeError(
                f"Cannot serialize an array with dtype {arr.dtype.name}"
                " as a _NumpyTensor."
            ) from e
        return cls(data=arr, numpy_dtype=arr.dtype.str, torch_dtype=torch_dtype)

    def to_tensor(self) -> torch.Tensor:
        """
        Converts a `_NumpyTensor` to a ``torch.Tensor`` and reifies any opaque
        data into the correct torch dtype.

        Returns:
            A ``torch.Tensor`` referring to the same data as the `data` field,
            with a correct torch dtype.
        """
        if not self.is_opaque:
            return torch.from_numpy(self.data)
        else:
            if not self.torch_dtype:
                raise ValueError(
                    "Tried to decode a tensor stored as opaque data, but no"
                    " torch dtype was specified"
                )
            tensor_view = torch.from_numpy(self.data)
            return tensor_view.view(self._decode_torch_dtype())

    @property
    def is_opaque(self):
        """
        Whether the ``self.data`` numpy array is opaque,
        i.e. stored as generic data without a meaningful dtype.

        Returns:
            True if ``self.data`` is uninterpretable without conversion
            to a tensor via `self.to_tensor()`, False otherwise.
        """
        return self._is_opaque(self.numpy_dtype)

    @staticmethod
    def _intermediate_type(size: int) -> torch.dtype:
        """
        Find a dtype to masquerade as that torch can convert to a numpy array.

        Args:
            size: The size of the dtype, in bytes.

        Returns:
            A ``torch.dtype`` for a tensor that torch can convert
            to a numpy array via ``tensor.numpy()``.
        """
        try:
            return _INTERMEDIATE_MAPPING[size]
        except KeyError as e:
            raise ValueError(
                "Cannot create a numpy array with opaque elements of size"
                f" {size} bytes"
            ) from e

    @classmethod
    def _is_opaque(cls, numpy_dtype: str) -> bool:
        """
        A check to see if the dtype needs to be swapped while decoding,
        based on whether the encoded dtype is in the opaque format
        used by this class.

        Args:
            numpy_dtype: The numpy dtype, as encoded in a tensorized file.

        Returns:
            True if the encoded dtype is opaque, False otherwise.
        """
        return numpy.dtype(numpy_dtype).type == numpy.void

    @classmethod
    def _is_asymmetric(cls, torch_dtype: torch.dtype) -> bool:
        """
        A check to see if the dtype needs to be swapped while encoding,
        based on whether numpy has a corresponding dtype or not.
        This check is hardcoded, not dynamic, but up to date as of torch 2.0.

        Args:
            dtype: The torch dtype to check

        Returns:
            True if a class is known not to have a corresponding numpy dtype,
            False otherwise.
        """
        return torch_dtype in _ASYMMETRIC_TYPES

    @classmethod
    def _decoder_dtype(cls, numpy_dtype: str):
        """
        Converts an opaque storage numpy dtype generated by this class
        into one that numpy can properly decode.

        NB: Even though a dtype like ``numpy.dtype("<V2")`` is valid,
        referring to the ``void16`` pseudo-type, numpy does not respect
        the endianness indicated in the type string when loading this way.
        If changed from ``<V2`` to ``<i2`` to load it as an int, it works fine.

        Args:
            numpy_dtype: The encoded numpy dtype.

        Returns:
            A dtype suitable for passing to numpy
        """
        if cls._is_opaque(numpy_dtype):
            return numpy_dtype.replace("V", "i")
        else:
            return numpy_dtype

    def _decode_torch_dtype(self) -> torch.dtype:
        """
        Parses the `self.torch_dtype` field.

        Returns: An instance of ``torch.dtype`` corresponding to the string
            stored in `self.torch_dtype`.

        Raises:
            ValueError: If `self.torch_dtype` is not set, is not in the form
                "torch.<dtype>", cannot be found in torch, or refers to
                something other than a ``torch.dtype``.
            TypeError: If `self.torch_dtype` is not a string.
        """
        # Quick route, table lookup for common types
        dtype = _DECODE_MAPPING.get(self.torch_dtype)
        if dtype is not None:
            return dtype
        else:
            # Long route using getattr(), any other type
            if not self.torch_dtype:
                raise ValueError("Cannot decode an empty dtype.")
            if not isinstance(self.torch_dtype, str):
                raise TypeError("torch_dtype must be a string.")
            module, *dtype_name = self.torch_dtype.split(".", 1)

            # Ensure that it's actually "torch.something"
            if module != "torch" or len(dtype_name) != 1:
                raise ValueError(f"Invalid torch_dtype: {self.torch_dtype}")

            dtype = getattr(torch, dtype_name[0])
            # Ensure that it's a real dtype
            if dtype is None or not isinstance(dtype, torch.dtype):
                raise ValueError(f"Invalid torch_dtype: {self.torch_dtype}")

            return dtype
