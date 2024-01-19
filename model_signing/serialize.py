# Copyright Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing perepo_managerissions and
# limitations under the License.

import hashlib
import base64
import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_start_method, set_start_method
from pathlib import Path
import platform
from typing import Callable

from _manifest import PathMetadata, DigestAlgorithm, Hashed

# Use for testing while keeping disk size low.
allow_symlinks = False


class Hasher:
    @staticmethod
    def node_header(name: str, ty: str) -> bytes:
        header = ty.encode('utf-8') + b'.' + \
            base64.b64encode(name.encode('utf-8')) + b'.'
        return header

    @staticmethod
    def root_folder(path: Path, content: bytes) -> str:
        return Hasher._node_folder_compute(name="root", content=content)

    @staticmethod
    def node_folder(path: Path, content: bytes) -> str:
        return Hasher._node_folder_compute(name=path.name, content=content)

    @staticmethod
    def _node_folder_compute(name: str, content: bytes) -> bytes:
        value = Hasher.node_header(name, "dir") + content
        return hashlib.sha256(value).digest()

    @staticmethod
    def root_file(path: Path, chunk: int) -> bytes:
        return Hasher._node_file_compute(path, b'', chunk)

    @staticmethod
    def node_file(path: Path, chunk: int = 0) -> bytes:
        if not path.is_file():
            raise ValueError(f"path {path} is not a file")
        header = Hasher.node_header(path.name, "file")
        return Hasher._node_file_compute(path, header, chunk)

    @staticmethod
    def _node_file_compute(path: Path, header: bytes, chunk: int) -> bytes:
        h = hashlib.sha256(header)
        with open(path, "rb") as f:
            if chunk == 0:
                all_data = f.read()
                h.update(all_data)
            else:
                # Compute the hash by reading chunk bytes at a time.
                while True:
                    chunk_data = f.read(chunk)
                    if not chunk_data:
                        break
                    h.update(chunk_data)
        return h.digest()

    @staticmethod
    def _node_file_compute_v1(path: Path, header: bytes,
                              start: int, end: int, chunk: int) -> bytes:
        h = hashlib.sha256(header)
        with open(path, "rb") as f:
            # WARNING: We must start reading the file at the starting offset.
            f.seek(start)
            # Read all at once.
            if chunk == 0 or chunk >= (end - start):
                content = f.read(end - start)
                # print(f"all: {f.name}: {start}-{end}")
                h.update(content)
            else:
                # Compute the hash by reading chunk bytes at a time.
                remains = end - start
                while remains != 0:
                    # read = (end - start) - remains
                    # print(f"loop {i}: {f.name}:
                    # {read}-{read + min(chunk, remains)}")
                    processed = min(chunk, remains)
                    chunk_data = f.read(processed)
                    if processed != len(chunk_data):
                        raise ValueError("internal: unread bytes: " +
                                         f"{processed} != {len(chunk_data)}")
                    if not chunk_data:
                        raise ValueError("internal: no data: " +
                                         f"filename={str(path)}, " +
                                         f"remains={remains}, " +
                                         f"{processed} != {len(chunk_data)}")
                    h.update(chunk_data)
                    remains -= processed
        return h.digest()


def remove_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def _validate_signature_path(model_path: Path, sig_path: Path):
    if model_path.is_file():
        return
    # Note: Only allow top-level folder to have the signature for simplicity.
    if sig_path is not None and sig_path.is_relative_to(model_path) and \
       sig_path.parent != model_path:
        raise ValueError(f"{sig_path} must be in the folder root")


def is_relative_to(p: Path, path_list: [Path]) -> bool:
    for e in path_list:
        if p.is_relative_to(e):
            return True
    return False


# TODO(): add a context "AI model"?
class Serializer:
    @staticmethod
    # TODO: type of returned value.
    def _ordered_files(path: Path, ignorepaths: [Path], ignore_folder: bool = False) -> []:
        children: [Path]
        if path.is_file():
            children = [path]
        else:
            # NOTE: the parent (..) and current directory (.) are not present.
            # NOTE: this returns hidden files as well.
            # TODO: tests that this pattern reports all files,
            # regardless of their depth.
            children = sorted(path.glob("**/*"))

        filtered = []
        total_size = 0
        for child in children:
            if is_relative_to(child, ignorepaths):
                continue

            # To avoid bugs where we read the link rather than its target,
            # we don't allow symlinks for now.
            # NOTE: It seems that Python's read() *always* follows symlinks,
            # so it may be safe to allow them. (readlink() is the function
            # to read the link metadata).
            if not allow_symlinks and child.is_symlink():
                raise ValueError(f"{str(child)} is symlink")

            if not child.is_file() and not child.is_dir():
                raise ValueError(f"{str(child)} is not a dir or file")
            
            if ignore_folder and child.is_dir():
                continue

            # The recorded path must *not* contains the folder name,
            # since users may rename it.
            record_path = remove_prefix(
                str(child.as_posix()), str(path.as_posix() + '/'))
            record_type = "file" if child.is_file() else "dir"
            record_size = \
                os.path.getsize(str(child)) if record_type == "file" else 0
            filtered += [(record_path, record_type, record_size)]
            total_size += record_size
        return filtered

    @staticmethod
    # TODO: type of returned value.
    def _create_tasks(children: [], shard_size: int) -> [[]]:
        tasks = [[]] * 0
        curr_file = 0
        curr_pos = 0

        while True:
            # All files have been processed.
            if curr_file >= len(children):
                break

            name, typ, size = children[curr_file]

            # It's a directory.
            # NOTE: It is fast to compute the hash because there's no data
            # besides the name and the type.
            # TODO(#12): do we need this at all? This only matters
            # if we care about empty directories, since non-empty ones have
            # their file + path recorded.
            if typ == "dir":
                # Record the task.
                tasks += [(name, typ, 0, size)]
                curr_file += 1
                curr_pos = 0
                continue

            # It's a file.

            # Sanity checks.
            if size <= curr_pos and size > 0:
                raise ValueError(f"internal: size={size}, " +
                                 f"curr_pos={curr_pos} " +
                                 f"for {children[curr_file]}")

            # Compute the number of bytes to process.
            remains = size - curr_pos
            if remains < 0:
                raise ValueError(f"internal: remains is {remains}")
            processed = min(remains, shard_size)
            end_pos = curr_pos + processed

            # Record the task.
            tasks += [(name, typ, curr_pos, end_pos)]

            # Update position.
            curr_pos += processed

            # If we have processed all bytes, we move on to the next file.
            if remains == processed:
                curr_file += 1
                curr_pos = 0
        return tasks

    @staticmethod
    # TODO: type of tasks
    def _run_tasks(path: Path, chunk: int, tasks: [], fn: Callable[[], bytes]) -> bytes:
        # See https://superfastpython.com/processpoolexecutor-in-python/
        # NOTE: 32 = length of sha256 digest.
        digest_len = 32
        all_hashes = [None] * (digest_len*len(tasks))
        org_len = len(all_hashes)

        # Use fork on Linux as it's supposed to be faster.
        if platform.system() == "Linux" and get_start_method() != "fork":
            set_start_method('fork')
        with ProcessPoolExecutor() as ppe:
            futures = [ppe.submit(fn, (path, chunk, task))
                       for task in tasks]
            results = [f.result() for f in futures]
            for i, result in enumerate(results):
                all_hashes[i*digest_len:(i+1)*digest_len] = result
        # Sanity check.
        if len(all_hashes) != org_len:
            raise ValueError(f"internal: {len(all_hashes)} != {org_len}")
        return bytes(all_hashes)

    @staticmethod
    # TODO: type of task_info.
    def _task_v1(task_info: any) -> bytes:
        # NOTE: we can get process info using:
        # from multiprocessing import current_process
        # worker = current_process()
        # print(f'Task {task_info},
        # worker name={worker.name}, pid={worker.pid}', flush=True)

        model_path, chunk, (name, ty, start_pos, end_pos) = task_info

        # Header format is: "type.b64(filename).start-end."
        header = ty.encode('utf-8') + b'.' + \
            base64.b64encode(name.encode('utf-8')) + \
            b'.' + f"{start_pos}-{end_pos}".encode('utf-8') + b'.'

        # To hash a directory, we use "none" content.
        # TODO(#12): do we need this at all? This only matters
        # if we care about empty directories, since non-empty ones have
        # their file + path recorded.
        if ty == "dir":
            value = header + b'none'
            return hashlib.sha256(value).digest()

        # We need to hash a file.

        # The model is a directory.
        if model_path.is_dir():
            return Hasher._node_file_compute_v1(model_path.joinpath(name),
                                                header, start_pos,
                                                end_pos, chunk)

        # The model is a single file.
        # We update the file name to a generic "root".
        header = ty.encode('utf-8') + b'.' + \
            base64.b64encode("root".encode('utf-8')) + \
            b'.' + f"{start_pos}-{end_pos}".encode('utf-8') + b'.'
        return Hasher._node_file_compute_v1(name,
                                            header, start_pos, end_pos, chunk)

    @staticmethod
    def _serialize_v1(path: Path, chunk: int, shard: int, signature_path: Path,
                      ignorepaths: [Path] = []) -> bytes:
        if not path.exists():
            raise ValueError(f"{str(path)} does not exist")

        if not allow_symlinks and path.is_symlink():
            raise ValueError(f"{str(path)} is a symlink")

        if chunk < 0:
            raise ValueError(f"{str(chunk)} is invalid")

        if not path.is_file() and not path.is_dir():
            raise ValueError(f"{str(path)} is not a dir or file")

        # Validate the signature path.
        _validate_signature_path(path, signature_path)

        # Children to hash.
        children = Serializer._ordered_files(path,
                                             [signature_path] + ignorepaths)

        # We shard the computation by creating independent "tasks".
        if shard < 0:
            raise ValueError(f"{str(shard)} is invalid")
        tasks = Serializer._create_tasks(children, shard)

        # Share the computation of hashes.
        # For simplicity, we pre-allocate the entire array that will hold
        # the concatenation of all hashes.
        all_hashes = Serializer._run_tasks(path, chunk, tasks, Serializer._task_v1)

        # Finally, we hash everything.
        return hashlib.sha256(bytes(all_hashes)).digest()

    @staticmethod
    # TODO: type of task_info.
    def _task_v2(task_info: any) -> bytes:
        # NOTE: we can get process info using:
        # from multiprocessing import current_process
        # worker = current_process()
        # print(f'Task {task_info},
        # worker name={worker.name}, pid={worker.pid}', flush=True)
        _, chunk, (name, ty, start_pos, end_pos) = task_info
        # Only files are recorded.
        if ty != "file":
            raise ValueError(f"internal: got a non-file path {name}")
        
        return Hasher._node_file_compute_v1(name,
                                            b'', start_pos, end_pos, chunk)

    @staticmethod
    def _to_path_metadata(task_info: [any], all_hashes: bytes) -> [PathMetadata]:
        if not task_info:
            raise ValueError("internal: task_info is empty")

        paths: [PathMetadata] = []
        # Iterate over all tasks.
        prev_task = task_info[0]
        prev_i = 0
        prev_name, _, _, _ = prev_task
        for curr_i, curr_task in enumerate(task_info[1:]):
            curr_name, _, _, _ = curr_task
            if prev_name == curr_name:
                continue
            # End of a group of sharded digests for the same file.
            # NOTE: each digest is 32-byte long.
            h = hashlib.sha256(bytes(all_hashes[prev_i: curr_i+32])).digest()
            paths += [PathMetadata(prev_name, Hashed(DigestAlgorithm.SHA256_P1, h))]
            prev_i = curr_i
            prev_name = curr_name

        # Compute the digest for the last (unfinished) task.
        if prev_i < len(task_info):
            h = hashlib.sha256(bytes(all_hashes[prev_i:])).digest()
            paths += [PathMetadata(prev_name, Hashed(DigestAlgorithm.SHA256_P1, h))]
        # paths += [PathMetadata("path/to/file1", Hashed(DigestAlgorithm.SHA256_P1, b'\abcdef1'))]
        # paths += [PathMetadata("path/to/file2", Hashed(DigestAlgorithm.SHA256_P1, b'\abcdef2'))]
        return paths

    @staticmethod
    def _serialize_v2(path: Path, chunk: int, shard: int, signature_path: Path,
                      ignorepaths: [Path] = []) -> bytes:
        if not path.exists():
            raise ValueError(f"{str(path)} does not exist")

        if not allow_symlinks and path.is_symlink():
            raise ValueError(f"{str(path)} is a symlink")

        if chunk < 0:
            raise ValueError(f"{str(chunk)} is invalid")

        if not path.is_file() and not path.is_dir():
            raise ValueError(f"{str(path)} is not a dir or file")

        # Validate the signature path.
        _validate_signature_path(path, signature_path)

        # Children to hash.
        children = Serializer._ordered_files(path,
                                             [signature_path] + ignorepaths,
                                             True)

        # We shard the computation by creating independent "tasks".
        if shard < 0:
            raise ValueError(f"{str(shard)} is invalid")
        tasks = Serializer._create_tasks(children, shard)

        # Share the computation of hashes.
        # For simplicity, we pre-allocate the entire array that will hold
        # the concatenation of all hashes.
        all_hashes = Serializer._run_tasks(path, chunk, tasks, Serializer._task_v2)

        # Turn hashes into PathMedata
        return Serializer._to_path_metadata(tasks, all_hashes)

    def serialize_v2(path: Path, chunk: int, signature_path: Path,
                     ignorepaths: [Path] = []) -> [PathMetadata]:
        # NOTE: The shard size must be the same for all clients for
        # compatibility. We could make it configurable; but in this
        # case the signature file must contain the value used by the signer.
        shard_size = 1000000000  # 1GB
        return Serializer._serialize_v2(path, chunk, shard_size,
                                        signature_path, ignorepaths)

    def serialize_v1(path: Path, chunk: int, signature_path: Path,
                     ignorepaths: [Path] = []) -> bytes:
        # NOTE: The shard size must be the same for all clients for
        # compatibility. We could make it configurable; but in this
        # case the signature file must contain the value used by the signer.
        shard_size = 1000000000  # 1GB
        return Serializer._serialize_v1(path, chunk, shard_size,
                                        signature_path, ignorepaths)

    @staticmethod
    def serialize_v0(path: Path, chunk: int, signature_path: Path,
                     ignorepaths: [Path] = []) -> bytes:
        if not path.exists():
            raise ValueError(f"{str(path)} does not exist")

        if not allow_symlinks and path.is_symlink():
            raise ValueError(f"{str(path)} is a symlink")

        if chunk < 0:
            raise ValueError(f"{str(chunk)} is invalid")

        if path.is_file():
            return Hasher.root_file(path, chunk)

        if not path.is_dir():
            raise ValueError(f"{str(path)} is not a dir")

        # Validate the signature path.
        _validate_signature_path(path, signature_path)

        children = sorted([x for x in path.iterdir()
                           if x != signature_path and x not in ignorepaths])
        # TODO: remove this special case?
        if len(children) == 0:
            return Hasher.root_folder(path, b"empty")

        hash = hashlib.sha256()
        for child in children:
            child_hash = Serializer._serialize_node(child, chunk, " ",
                                                    ignorepaths)
            hash.update(child_hash)
        content = hash.digest()
        return Hasher.root_folder(path, content)

    @staticmethod
    def _serialize_node(path: Path, chunk: int, indent="",
                        ignorepaths: [Path] = []) -> bytes:
        if not allow_symlinks and path.is_symlink():
            raise ValueError(f"{str(path)} is a symlink")

        if path.is_file():
            return Hasher.node_file(path, chunk)

        if not path.is_dir():
            raise ValueError(f"{str(path)} is not a dir")

        children = sorted([x for x in path.iterdir() if x not in ignorepaths])
        # TODO: remove this special case?
        if len(children) == 0:
            return Hasher.node_folder(path, b"empty")

        hash = hashlib.sha256()
        for child in children:
            child_hash = Serializer._serialize_node(child, chunk, indent + " ",
                                                    ignorepaths)
            hash.update(child_hash)
        content = hash.digest()
        return Hasher.node_folder(path, content)
