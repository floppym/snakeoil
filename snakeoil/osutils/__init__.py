# Copyright 2004-2010 Brian Harring <ferringb@gmail.com>
# Copyright 2006 Marien Zwart <marienz@gentoo.org>
# License: BSD/GPL2

"""
OS related functionality

This module is primarily optimized implementations of various filesystem operations,
written for posix specifically.  If this is a non-posix system (or extensions were
disabled) it falls back to native python implementations that yield no real speed gains.

A rough example of the performance benefits, collected from a core2 2.4GHz running
python 2.6.5, w/ an EXT4 FS on a 160GB x25-M for the FS related invocations (it's worth
noting the IO is pretty fast in this setup- for slow IO like nfs, the speedup for extension
vs native for listdir* functionality is a fair bit larger).

Rough stats:

========================================================  =========   ===============
python -m timeit code snippet                             native      extension time
========================================================  =========   ===============
join("/usr/portage", "dev-util", "bsdiff", "ChangeLog")   2.8 usec    0.36 usec
normpath("/usr/portage/foon/blah/dar")                    5.52 usec   0.15 usec
normpath("/usr/portage//foon/blah//dar")                  5.66 usec   0.15 usec
normpath("/usr/portage/./foon/../blah/")                  5.92 usec   0.15 usec
listdir_files("/usr/lib64") # 2338 entries, 990 syms      18.6 msec   4.17 msec
listdir_files("/usr/lib64", False) # same dir content     16.9 msec   1.48 msec
readfile("/etc/passwd") # 1899 bytes                      20.4 usec   4.05 usec
readfile("tmp-file") # 1MB                                300 usec    259 usec
list(readlines("/etc/passwd")) # 1899 bytes, 34 lines     37.3 usec   12.8 usec
list(readlines("/etc/passwd", False)) # leave whitespace  26.7 usec   12.8 usec
========================================================  =========   ===============

If you're just invoking join or normpath, or reading a file or two a couple of times,
these optimizations are probably overkill.  If you're doing lots of path manipulation,
reading files, scanning directories, etc, these optimizations start adding up
pretty quickly.
"""

__all__ = ['abspath', 'abssymlink', 'ensure_dirs', 'join', 'pjoin',
    'listdir_files', 'listdir_dirs', 'listdir',
    'readdir', 'normpath', 'unlink_if_exsts',
    'FsLock', 'GenericFailed',
    'LockException', 'NonExistant']
__all__.extend("%s%s" % ('readfile', mode) for mode in
        ['', '_ascii', '_ascii_strict', '_bytes', '_utf8'])
__all__.extend("%s%s" % ('readlines', mode) for mode in
        ['', '_ascii', '_ascii_strict', '_bytes', '_utf8', '_utf8_strict'])

__all__ = tuple(__all__)

import os, stat
import fcntl
import errno

# No name '_readdir' in module osutils
# pylint: disable-msg=E0611

try:
    from snakeoil.osutils import _readdir as module
except ImportError:
    from snakeoil.osutils import native_readdir as module

# delay this... it's a 1ms hit, and not a lot of the consumers
# force utf8 codepaths yet.
from snakeoil import compatibility
from snakeoil.weakrefs import WeakRefFinalizer
from snakeoil.demandload import demandload
demandload(globals(), "codecs")
from snakeoil.currying import partial, pretty_docs

listdir = module.listdir
listdir_dirs = module.listdir_dirs
listdir_files = module.listdir_files
readdir = module.readdir

del module

def _safe_mkdir(path, mode):
    try:
        os.mkdir(path, mode)
    except OSError, e:
        # if it exists already and is a dir, non issue.
        if e.errno != errno.EEXIST:
            return False
        if not stat.S_ISDIR(os.stat(path).st_mode):
            return False
    return True

def ensure_dirs(path, gid=-1, uid=-1, mode=0777, minimal=True):
    """
    ensure dirs exist, creating as needed with (optional) gid, uid, and mode.

    be forewarned- if mode is specified to a mode that blocks the euid
    from accessing the dir, this code *will* try to create the dir.

    :param path: directory to ensure exists on disk
    :param gid: a valid GID to set any created directories to
    :param uid: a valid UID to set any created directories to
    :param mode: permissions to set any created directories to
    :param minimal: boolean controlling whether or not the specified mode
        must be enforced, or is the minimal permissions necessary.  For example,
        if mode=0755, minimal=True, and a directory exists with mode 0707,
        this will restore the missing group perms resulting in 757.
    :return: True if the directory could be created/ensured to have those
        permissions, False if not.
    """

    try:
        st = os.stat(path)
    except OSError:
        base = os.path.sep
        try:
            um = os.umask(0)
            # if the dir perms would lack +wx, we have to force it
            force_temp_perms = ((mode & 0300) != 0300)
            resets = []
            apath = normpath(os.path.abspath(path))
            sticky_parent = False

            for directory in apath.split(os.path.sep):
                base = join(base, directory)
                try:
                    st = os.stat(base)
                    if not stat.S_ISDIR(st.st_mode):
                        return False

                    # if it's a subdir, we need +wx at least
                    if apath != base:
                        if ((st.st_mode & 0300) != 0300):
                            try:
                                os.chmod(base, (st.st_mode | 0300))
                            except OSError:
                                return False
                            resets.append((base, st.st_mode))
                        sticky_parent = (st.st_gid & stat.S_ISGID)

                except OSError:
                    # nothing exists.
                    try:
                        if force_temp_perms:
                            if not _safe_mkdir(base, 0700):
                                return False
                            resets.append((base, mode))
                        else:
                            if not _safe_mkdir(base, mode):
                                return False
                            if base == apath and sticky_parent:
                                resets.append((base, mode))
                            if gid != -1 or uid != -1:
                                os.chown(base, uid, gid)
                    except OSError:
                        return False

            try:
                for base, m in reversed(resets):
                    os.chmod(base, m)
                if uid != -1 or gid != -1:
                    os.chown(base, uid, gid)
            except OSError:
                return False

        finally:
            os.umask(um)
        return True
    else:
        try:
            if ((gid != -1 and gid != st.st_gid) or
                (uid != -1 and uid != st.st_uid)):
                os.chown(path, uid, gid)
            if minimal:
                if mode != (st.st_mode & mode):
                    os.chmod(path, st.st_mode | mode)
            elif mode != (st.st_mode & 07777):
                os.chmod(path, mode)
        except OSError:
            return False
    return True


def abssymlink(path):
    """
    Return the absolute path of a symlink

    :param path: filepath to resolve
    :return: resolved path
    :raise: EnvironmentError, errno=ENINVAL if the requested path isn't
        a symlink
    """
    mylink = os.readlink(path)
    if mylink[0] != '/':
        mydir = os.path.dirname(path)
        mylink = mydir+"/"+mylink
    return normpath(mylink)


def abspath(path):
    """
    resolve a path absolutely, including symlink resolving.

    Note that if it's a symlink and the target doesn't exist, it'll still
    return the target.

    :param path: filepath to resolve.
    :raise: EnvironmentError some errno other than an ENOENT or EINVAL
        is encountered
    :return: the absolute path calculated against the filesystem
    """
    path = os.path.abspath(path)
    try:
        return abssymlink(path)
    except EnvironmentError, e:
        if e.errno not in (errno.ENOENT, errno.EINVAL):
            raise
        return path


def native_normpath(mypath):
    """
    normalize path- //usr/bin becomes /usr/bin, /usr/../bin becomes /bin

    see :py:func:`os.path.normpath` for details- this function differs from
    `os.path.normpath` only in that it'll convert leading '//' into '/'
    """
    newpath = os.path.normpath(mypath)
    if newpath.startswith('//'):
        return newpath[1:]
    return newpath

native_join = os.path.join

def _internal_native_readfile(mode, mypath, none_on_missing=False, encoding=None,
    strict=compatibility.is_py3k):
    """
    read a file, returning the contents

    :param mypath: fs path for the file to read
    :param none_on_missing: whether to return None if the file is missing,
        else through the exception
    """
    try:
        if encoding and strict:
            # we special case this- codecs.open is about 2x slower,
            # thus if py3k, use the native one (which supports encoding directly)
            if compatibility.is_py3k:
                return open(mypath, mode, encoding=encoding).read()
            return codecs.open(mypath, mode, encoding=encoding).read()
        return open(mypath, mode).read()
    except IOError, oe:
        if none_on_missing and oe.errno == errno.ENOENT:
            return None
        raise

def _mk_pretty_derived_func(func, name_base, name, *args, **kwds):
    if name:
        name = '_' + name
    return pretty_docs(partial(func, *args, **kwds),
        name='%s%s' % (name_base, name))

_mk_readfile = partial(_mk_pretty_derived_func, _internal_native_readfile,
    'readfile')

native_readfile_ascii = _mk_readfile('ascii', 'rt')
native_readfile = native_readfile_ascii
native_readfile_ascii_strict = _mk_readfile('ascii_strict', 'r',
    encoding='ascii', strict=True)
native_readfile_bytes = _mk_readfile('bytes', 'rb')
native_readfile_utf8 = _mk_readfile('utf8', 'r',
    encoding='utf8', strict=False)
native_readfile_utf8_strict = _mk_readfile('utf8_strict', 'r',
    encoding='utf8', strict=True)

class readlines_iter(object):
    __slots__ = ("iterable", "mtime")
    def __init__(self, iterable, mtime):
        self.iterable = iterable
        self.mtime = mtime

    def __iter__(self):
        return self.iterable

def _py2k_ascii_strict_filter(source):
    any = compatibility.any
    for line in source:
        if any((0x80 & ord(char)) for char in line):
            raise ValueError("character ordinal over 127");
        yield line


def _strip_whitespace_filter(iterable):
    for line in iterable:
        yield line.strip()


def native_readlines(mode, mypath, strip_whitespace=True, swallow_missing=False,
    none_on_missing=False, encoding=None, strict=compatibility.is_py3k):
    """
    read a file, yielding each line

    :param mypath: fs path for the file to read
    :param strip_whitespace: strip any leading or trailing whitespace including newline?
    :param swallow_missing: throw an IOError if missing, or swallow it?
    :param none_on_missing: if the file is missing, return None, else
        if the file is missing return an empty iterable
    """
    handle = iterable = None
    try:
        if encoding and strict:
            # we special case this- codecs.open is about 2x slower,
            # thus if py3k, use the native one (which supports encoding directly)
            if compatibility.is_py3k:
                handle = open(mypath, mode, encoding=encoding)
            else:
                handle = codecs.open(mypath, mode, encoding=encoding)
                if encoding == 'ascii':
                    iterable = _py2k_ascii_strict_filter(handle)
        else:
            handle = open(mypath, mode)
    except IOError, ie:
        if ie.errno != errno.ENOENT or not swallow_missing:
            raise
        if none_on_missing:
            return None
        return readlines_iter(iter([]), None)

    mtime = os.fstat(handle.fileno()).st_mtime
    if not iterable:
        iterable = iter(handle)
    if not strip_whitespace:
        return readlines_iter(iterable, mtime)
    return readlines_iter(_strip_whitespace_filter(iterable), mtime)


_mk_readlines = partial(_mk_pretty_derived_func, native_readlines,
    'readlines')

try:
    from snakeoil.osutils._posix import normpath, join, readfile, readlines
    readfile_ascii = readfile
    readlines_ascii = readlines
except ImportError:
    normpath = native_normpath
    join = native_join
    readfile_ascii = native_readfile_ascii
    readfile = native_readfile
    readlines_ascii = _mk_readlines('ascii', 'r',
        encoding='ascii')
    readlines = readlines_ascii

readlines_bytes = _mk_readlines('bytes', 'rb')
readlines_ascii_strict = _mk_readlines('ascii_strict', 'r',
    encoding='ascii', strict=True)
readlines_utf8 = _mk_readlines('utf8', 'r', encoding='utf8')
readlines_utf8_strict = _mk_readlines('utf8_strict', 'r',
    encoding='utf8', strict=True)


readfile_ascii_strict = native_readfile_ascii_strict
readfile_bytes = native_readfile_bytes
readfile_utf8 = native_readfile_utf8
readfile_utf8_strict = native_readfile_utf8_strict

# convenience.  importing join into a namespace is ugly, pjoin less so
pjoin = join

class LockException(Exception):
    """Base lock exception class"""
    def __init__(self, path, reason):
        Exception.__init__(self, path, reason)
        self.path, self.reason = path, reason

class NonExistant(LockException):
    """Missing file/dir exception"""

    def __init__(self, path, reason=None):
        LockException.__init__(self, path, reason)

    def __str__(self):
        return (
            "Lock action for '%s' failed due to not being a valid dir/file %s"
            % (self.path, self.reason))

class GenericFailed(LockException):
    """The fallback lock exception class.

    Covers perms, IOError's, and general whackyness.
    """
    def __str__(self):
        return "Lock action for '%s' failed due to '%s'" % (
            self.path, self.reason)


# should the fd be left open indefinitely?
# IMO, it shouldn't, but opening/closing everytime around is expensive


class FsLock(object):

    """
    fnctl based filesystem lock
    """

    __metaclass__ = WeakRefFinalizer
    __slots__ = ("path", "fd", "create")

    def __init__(self, path, create=False):
        """
        :param path: fs path for the lock
        :param create: controls whether the file will be created
            if the file doesn't exist.
            If true, the base dir must exist, and it will create a file.
            If you want to lock via a dir, you have to ensure it exists
            (create doesn't suffice).
        :raise NonExistant: if no file/dir exists for that path,
            and cannot be created
        """
        self.path = path
        self.fd = None
        self.create = create
        if not create:
            if not os.path.exists(path):
                raise NonExistant(path)

    def _acquire_fd(self):
        if self.create:
            try:
                self.fd = os.open(self.path, os.R_OK|os.O_CREAT)
            except OSError, oe:
                raise GenericFailed(self.path, oe)
        else:
            try:
                self.fd = os.open(self.path, os.R_OK)
            except OSError, oe:
                raise NonExistant(self.path, oe)

    def _enact_change(self, flags, blocking):
        if self.fd is None:
            self._acquire_fd()
        # we do it this way, due to the fact try/except is a bit of a hit
        if not blocking:
            try:
                fcntl.flock(self.fd, flags|fcntl.LOCK_NB)
            except IOError, ie:
                if ie.errno == errno.EAGAIN:
                    return False
                raise GenericFailed(self.path, ie)
        else:
            fcntl.flock(self.fd, flags)
        return True

    def acquire_write_lock(self, blocking=True):
        """
        Acquire an exclusive lock

        Note if you have a read lock, it implicitly upgrades atomically

        :param blocking: if enabled, don't return until we have the lock
        :return: True if lock is acquired, False if not.
        """
        return self._enact_change(fcntl.LOCK_EX, blocking)

    def acquire_read_lock(self, blocking=True):
        """
        Acquire a shared lock

        Note if you have a write lock, it implicitly downgrades atomically

        :param blocking: if enabled, don't return until we have the lock
        :return: True if lock is acquired, False if not.
        """
        return self._enact_change(fcntl.LOCK_SH, blocking)

    def release_write_lock(self):
        """Release an write/exclusive lock if held"""
        self._enact_change(fcntl.LOCK_UN, False)

    def release_read_lock(self):
        """Release an shared/read lock if held"""
        self._enact_change(fcntl.LOCK_UN, False)

    def __del__(self):
        # alright, it's 5:45am, yes this is weird code.
        try:
            if self.fd is not None:
                self.release_read_lock()
        finally:
            if self.fd is not None:
                os.close(self.fd)


def fallback_access(path, mode, root=0):
    try:
        st = os.lstat(path)
    except EnvironmentError:
        return False
    if mode == os.F_OK:
        return True
    # rules roughly are as follows; if process uid == file uid, those perms
    # apply.
    # if groups match... that perm group is the fallback (authorative)
    # if neither, then other
    # if root, w/r is guranteed, x is actually checked
    # note posix says X_OK can be True, which is a worthless result, hence this
    # fallback for systems that take advantage of that posix misfeature.

    myuid = os.getuid()

    # if we're root... pull out X_OK and check that alone.  the rules of
    # X_OK under linux (which this function emulates) are that any +x is a True
    # as for WR, that's always allowed (well not always- selinux may change that)

    if myuid == 0:
        mode &= os.X_OK
        if not mode:
            # w/r are always True for root, so return up front
            return True
        # py3k doesn't like octal syntax; this is 0111
        return bool(st.st_mode & 73)

    mygroups = os.getgroups()

    if myuid == st.st_uid:
        # shift to the user octet, filter to 3 bits, verify intersect.
        return mode == (mode & ((st.st_mode >> 6) & 0x7))
    if st.st_gid in mygroups:
        return mode == (mode & ((st.st_mode >> 3) & 0x7))
    return mode == (mode & (st.st_mode & 0x7))

fallback_access.__doc__ = getattr(os.access, '__doc__', None)

if 'sunos' == os.uname()[0].lower():
    # XXX snakeoil needs to grow a "steal the docs from another function"
    access = fallback_access
    access.__name__ = 'access'
else:
    access = os.access

def unlink_if_exists(path):
    """
    wrap os.unlink, ignoring if the file doesn't exist

    :param path: a non directory target to ensure doesn't exist
    """

    try:
        os.unlink(path)
    except EnvironmentError, e:
        if e.errno != errno.ENOENT:
            raise
