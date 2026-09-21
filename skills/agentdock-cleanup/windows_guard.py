"""Windows single-file deletion with pinned ancestors and an exclusive file handle.

No path-based unlink or recursive directory deletion is used. This module does
not grant eligibility: the caller must revalidate policy, receipts and content.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


def _kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.GetFileInformationByHandleEx.restype = wintypes.BOOL
    k.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.SetFileInformationByHandle.restype = wintypes.BOOL
    return k


class AttributeTag(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class BasicInfo(ctypes.Structure):
    _fields_ = [("creation", ctypes.c_int64), ("access", ctypes.c_int64),
                ("write", ctypes.c_int64), ("change", ctypes.c_int64), ("attributes", wintypes.DWORD)]


def change_time(stream):
    # Python's Windows path stat and fstat may disagree about legacy st_ctime.
    # Use the native ChangeTime from the open handle consistently for receipts/plans.
    import msvcrt
    k = _kernel()
    info = BasicInfo()
    if not k.GetFileInformationByHandleEx(msvcrt.get_osfhandle(stream.fileno()), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.change * 100


def _check_handle(k, handle, expected, directory):
    info = AttributeTag()
    if not k.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
        raise OSError("Reparse point or unexpected file type")
    buf = ctypes.create_unicode_buffer(32768)
    length = k.GetFinalPathNameByHandleW(handle, buf, len(buf), 0)
    if not length or length >= len(buf):
        raise OSError("Cannot resolve handle path")
    final = buf.value
    if final.startswith("\\\\?\\"):
        final = final[4:]
    if os.path.normcase(os.path.normpath(final)) != os.path.normcase(os.path.normpath(str(expected))):
        raise OSError("Handle path differs from validated path")


class LockedFile:
    def __init__(self, kernel, handle, stream):
        self.kernel, self.handle, self.stream = kernel, handle, stream

    def delete(self):
        # FileDispositionInfo acts on this exact open file object, not a re-resolved pathname.
        delete_flag = ctypes.c_ubyte(1)
        if not self.kernel.SetFileInformationByHandle(self.handle, 4, ctypes.byref(delete_flag), ctypes.sizeof(delete_flag)):
            raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def locked_file(raw):
    if os.name != "nt":
        raise OSError("Windows required")
    import msvcrt
    p = Path(raw)
    k = _kernel()
    handles = []
    file_handle = None
    stream = None
    try:
        # Deny delete/rename sharing on every ancestor, acquired from volume down.
        for parent in reversed(p.parents):
            handle = k.CreateFileW(str(parent), 0x80, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            _check_handle(k, handle, parent, True)
        # GENERIC_READ | DELETE, share mode zero: no competing read/write/delete handles.
        file_handle = k.CreateFileW(str(p), 0x80000000 | 0x10000, 0, None, 3, 0x00200000, None)
        if file_handle == ctypes.c_void_p(-1).value:
            file_handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        _check_handle(k, file_handle, p, False)
        handle = file_handle
        fd = msvcrt.open_osfhandle(file_handle, os.O_RDONLY | os.O_BINARY)
        file_handle = None  # fd now owns the handle.
        stream = os.fdopen(fd, "rb")
        yield LockedFile(k, handle, stream)
    finally:
        if stream is not None:
            stream.close()
        if file_handle is not None:
            k.CloseHandle(file_handle)
        for handle in reversed(handles):
            k.CloseHandle(handle)
