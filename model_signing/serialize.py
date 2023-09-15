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

import hashlib, base64, os
from typing import IO
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import set_start_method
from pathlib import Path

class Hasher:
    @staticmethod
    def node_header(name: str, ty: str) -> bytes:
        header = ty.encode('utf-8') + b'.' + base64.b64encode(name.encode('utf-8')) + b'.'
        return header

    @staticmethod
    def root_folder(path: Path, content:bytes) -> str:
        return Hasher._node_folder_compute(name="root", content=content)
    
    @staticmethod
    def node_folder(path: Path, content:bytes) -> str:
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
        with open(path,"rb") as f:
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
    def _node_file_compute_v1(path: Path, header: bytes, start: int, end: int, chunk: int) -> bytes:
        h = hashlib.sha256(header)
        with open(path,"rb") as f:
            f.seek(start)
            # Read all at once.
            if chunk == 0 or chunk >= (end - start):
                content = f.read(end - start)
                #print(f"all: {f.name}: {start}-{end}")
                h.update(content)
            else:
                # Compute the hash by reading chunk bytes at a time.
                o_start = 0
                o_end = chunk # chunk is < total number of bytes to read.
                while True:
                    chunk_data = f.read(o_end - o_start)
                    #print(f"loop {o_start/chunk}: {f.name}: {start + o_start}-{start + o_end}")
                    # NOTE: len(chunk_data) may be < the request number of bytes (o_end - o_start)
                    # when we reach the EOF.
                    if not chunk_data:
                        break
                    h.update(chunk_data)
                    o_start += chunk
                    o_end += chunk

        return h.digest()

def remove_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix):]
    return text

def validate_signature_path(model_path: Path, sig_path: Path):
    if model_path.is_file():
        return
    # Note: Only allow top-level folder to have the signature for simplicity.
    if sig_path is not None and sig_path.is_relative_to(model_path) and sig_path.parent != model_path:
        raise ValueError(f"{sig_path} must be in the folder root")

# TODO(): add a context "AI model"?
class Serializer:
    @staticmethod
    # TODO: type of returned value.
    def _ordered_files(path: Path, ignorepaths: [Path]) -> []:
        children: [Path]
        if path.is_file():
            children = [path]
        else:
            # NOTE: the parent (..) and current directory (.) are not present.
            # NOTE: this returns hidden files as well.
            # TODO: tests that this pattern reports all files, regardless of their
            # depth.
            children = sorted(path.glob("**/*"))

        filtered = []
        total_size = 0
        for child in children:
            if child in ignorepaths:
                continue

            # To avoid bugs where we read the link rather than its target,
            # we don't allow symlinks for now.
            # NOTE: It seems that Python's read() *always* follows symlinks,
            # so it may be safe to allow them. (readlink() is the function to read the link metadata).
            if child.is_symlink():
                raise ValueError(f"{str(child)} is symlink")

            if not child.is_file() and not child.is_dir():
                raise ValueError(f"{str(child)} is not a dir or file")

            # The recorded path must *not* contains the folder name,
            # since users may rename it.
            record_path = remove_prefix(str(child), str(path.as_posix()) + os.sep)
            record_type = "file" if child.is_file() else "dir"
            record_size = os.path.getsize(str(child)) if record_type == "file" else 0
            filtered += [(record_path, record_type, record_size)]
            total_size += record_size
        return filtered

    @staticmethod
    # TODO: type of returned value.
    def _create_tasks(children :[], shard_size: int) -> [[]]:
        tasks = [[]] * 0
        curr_file = 0
        curr_bytes = 0
        total_bytes = 0

        while True:
            # All files have been processed.
            if curr_file >= len(children):
                break
            
            name, typ, size = children[curr_file]

            ### It's a directory.
            # NOTE: It is fast to commupte the hash because there's no data besides the name and the type.
            if typ == "dir":
                curr_bytes = 0
                tasks += [(name, typ, 0, size)]
                curr_file += 1
                continue

            ### It's a file.

            # Sanity checks.
            if size <= curr_bytes and size > 0:
                raise ValueError(f"internal: size={size}, curr_bytes={curr_bytes} for {children[curr_file]}")

            # Compute the number of bytes to process.
            start_pos = curr_bytes
            available_bytes = size - start_pos
            if available_bytes < 0:
                raise ValueError(f"internal: available_bytes is {available_bytes}")
            processed_bytes = min(available_bytes, shard_size)
            end_pos = curr_bytes + processed_bytes
            curr_bytes += processed_bytes
            total_bytes += processed_bytes

            # Record the task.
            tasks += [(name, typ, start_pos, end_pos)]

            # If we have processed all bytes, we move on to the next file.
            if available_bytes - processed_bytes == 0:
                curr_file += 1
                curr_bytes = 0

        return tasks

    @staticmethod
    # TODO: type of tasks
    def _run_tasks(path: Path, chunk: int, tasks :[]) -> bytes:
        # See https://superfastpython.com/processpoolexecutor-in-python/
        # NOTE: 32 = length of sha256 digest.
        digest_len = 32
        all_hashes = [None] * (digest_len*len(tasks))
        org_len = len(all_hashes)
                
        set_start_method('fork')
        with ProcessPoolExecutor() as ppe:
            futures = [ ppe.submit(Serializer.task, (path, chunk, tasks[i])) for i in range(len(tasks)) ]
            results = [ f.result() for f in futures ]
            for i in range(len(results)):
                all_hashes[i*digest_len:(i+1)*digest_len] = results[i]
        # Sanity check.
        if len(all_hashes) != org_len:
            raise ValueError(f"internal: {len(all_hashes)} != {org_len}")
        return bytes(all_hashes)

    @staticmethod
    # TODO: type of task_info.
    def task(task_info: []):
        # NOTE: we can get process info using:
        # from multiprocessing import current_process
        # worker = current_process()
        # print(f'Task {task_info}, worker name={worker.name}, pid={worker.pid}', flush=True)

        model_path, chunk, (name, ty, start_pos, end_pos) = task_info

        # Header format is: "type.b64(filename).start-end."
        header = ty.encode('utf-8') + b'.' + base64.b64encode(name.encode('utf-8')) + b'.' + f"{start_pos}-{end_pos}".encode('utf-8') + b'.'

        # To hash a directory, we use "empty" content.        
        if ty == "dir":
            value = header + b'empty'
            return hashlib.sha256(value).digest()

        # We need to hash a file.

        # The model is a directory.
        if model_path.is_dir():
            return Hasher._node_file_compute_v1(model_path.joinpath(name), header, start_pos, end_pos, chunk)
        
        # The model is a single file.
        return Hasher._node_file_compute_v1(name, header, start_pos, end_pos, chunk)

    @staticmethod
    def serialize_v1(path: Path, chunk: int, signature_path: Path, ignorepaths: [Path] = []) -> bytes:
        if not path.is_file() and not path.is_dir():
            raise ValueError(f"{str(path)} is not a dir or file")

        # Validate the signature path.
        validate_signature_path(path, signature_path)
        
        # Children to hash.
        children = Serializer._ordered_files(path, [signature_path] + ignorepaths)
        
        # We shard the computation by creating independent "tasks".
        shard_size = 1000000000 # 1GB
        tasks = Serializer._create_tasks(children, shard_size)
        for t in tasks:
            print (t)
        
        # Share the computation of hashes.
        # For simplicity, we pre-allocate the entire array that will hold
        # the concatenation of all hashes.
        all_hashes = Serializer._run_tasks(path, chunk, tasks)
        
        # Finally, we hash everything.
        return hashlib.sha256(bytes(all_hashes)).digest()

    @staticmethod
    def serialize_v0(path: Path, chunk: int, signature_path: Path, ignorepaths: [Path] = []) -> bytes:
        if path.is_file():
            return Hasher.root_file(path, chunk)

        if not path.is_dir():
            raise ValueError(f"{str(path)} is not a dir")

        # Validate the signature path.
        validate_signature_path(path, signature_path)

        children = sorted([x for x in path.iterdir() if x != signature_path and x not in ignorepaths])
        # TODO: remove this special case?
        if len(children) == 0:
            return Hasher.root_folder(path, b"empty")

        hash = hashlib.sha256()
        for child in children:
            child_hash = Serializer._serialize_node(child, chunk, " ")
            hash.update(child_hash)
        content = hash.digest()
        return Hasher.root_folder(path, content)
    
    @staticmethod
    def _serialize_node(path: Path, chunk: int, indent = "") -> bytes:
        if path.is_file():
            return Hasher.node_file(path, chunk)

        if not path.is_dir():
            raise ValueError(f"{str(path)} is not a dir")

        children = sorted([x for x in path.iterdir()])
        # TODO: remove this special case?
        if len(children) == 0:
            return Hasher.node_folder(path, b"empty")

        hash = hashlib.sha256()
        for child in children:
            child_hash = Serializer._serialize_node(child, chunk, indent + " ")
            hash.update(child_hash)
        content = hash.digest()
        return Hasher.node_folder(path, content)
