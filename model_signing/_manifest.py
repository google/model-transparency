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

from enum import Enum
from pathlib import Path
from typing import IO

import in_toto_attestation.v1.resource_descriptor_pb2 as rdpb
from in_toto_attestation.v1.statement import Statement

class DigestAlgorithm(Enum):
    SHA256_P1 = 1

    def __str__(self):
        return str(self.name).replace("_", "-").lower()
    @staticmethod
    def from_string(s: str):
        return DigestAlgorithm[s.replace("-", "_")]

class Hashed:
    algorithm: DigestAlgorithm
    digest: bytes
    def __init__(self, algorithm_: DigestAlgorithm, digest_: bytes):
        self.algorithm = algorithm_
        self.digest = digest_

class PathMetadata:
    hashed: Hashed
    path: str
    def __init__(self, path_: str, hashed_: Hashed):
        self.path = path_
        self.hashed = hashed_

class Manifest:
    paths: PathMetadata
    predicate_type: str
    def __init__(self, paths: [PathMetadata]):
        self.paths = paths
        self.predicate_type = "https://github.com/google/model-transparency/manifest/v1"

    def to_intoto_statement(self) -> Statement:
        subjects: [rdpb.ResourceDescriptor] = []
        for _, p in enumerate(self.paths):
            sub = rdpb.ResourceDescriptor()
            sub.download_location = p.path
            sub.digest[str(p.hashed.algorithm)] = p.hashed.digest.hex()
            subjects += [sub]
        # See example at https://github.com/in-toto/attestation/blob/main/python/tests/test_statement.py.
        return Statement(subjects, self.predicate_type, {})
