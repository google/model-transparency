# DIRSHA256 specifications

## Abstract

This document describes DIRSHA256, an parameterized algorithm to serialize the content of a filesystem path using the cryptographic hash function SHA256. The cryptographic strength of DIRSHA256 depends on the properties of the SHA256 hash function.

## Introduction

ML frameworks like PyTorch's and Hugging Face's store model parameters (weight, architecture, etc) in several files stored in a directory. These files can be several hundreds of gigabytes in size. Thus we need a fast and flexible serizalization mechanism to cryptographically hash a model stored in a directory.

## Terminology and symbols

`Path`: A file-system path.

`Directory`: A directory, comprising files and sub-directories stored on a file-system.

`POSIX`: The POSIX representation of a path, using the characcter `/` as a delimiter between elements of the path.

`Shard`: A partition of data.

`Shard size`: A positive integer defining the size of a shard partition.

`Digest`: The bytes output by a cryptographic hash function.

`CONCAT`: A shorthand for "bytes concatenation", ie the process of appending bytes one after the other.

`UTF-8`: The character encoding defined in [RFC-3629](https://datatracker.ietf.org/doc/html/rfc3629).

`SHA256`: The cryptographic hash function SHA256 defined by [NIST](https://csrc.nist.gov/projects/hash-functions).

`BASE64`: Base64 encoding as defined in [RFC-4648](https://datatracker.ietf.org/doc/html/rfc4648).

`ITOA`: A function that takes an unsigned integer and returns its string representation.

`MIN`: A function that returns the minimum value of its arguments.

`READ_FILE`: A function that takes a path, a starting offset and an end offset, and returns the content of the file from the starting to the end offset.

`DIRSHA256`: The name of the algorithm defined in this document.

`DIRSHA256-p1`: The DIRSHA256 algorithm instantiated with the parameter set `p1`.

## Specifications

DIRSHA256 takes as input a file-system path and a [shard size](#terminology-and-symbols) and outputs a 32-byte (256-bit) digest.

DIRSHA256 is comprised of four sub-routines: [Ordered Paths Generation (OPG)](#ordered-paths-generation-opg), [Hashing Task Generation (HTG)](#hashing-task-generation-htg), [Hashing Task Execution (HTE)](#hashing-task-execution-hte) and [Final Digest Computation (FDC)](#final-digest-computation-fdc).

### Ordered Paths Generation (OPG)

The OPG routine takes as input a model path and outputs a list of files and their metadata, ordered alphabetically by their path, where each character is represented as UTF-8:

```java
path_metadata struct {
    path // POSIX path representation with UTF-8 character encoding.
    type // Type of the path: "dir" or "file".
    size // The size of the path, in bytes. 0 for directories.
}

func OPG(path) -> []path_metadata
```

The function OPG MUST enforce the following invariants:

1. Each path is a non-empty absolute path starting at the root of the model path, represented in POSIX format with characters encoded in UTF-8.
1. The list is ordered alphabetically by path in ascending order. Characters are compared based on their UTF-8 representation.
1. Each path type is either "dir" or "file". Other types (e.g., symlinks) MUST produce an error and OPG MUST fail.
1. Empty directories MUST be present in the list.
1. Directories always have a size of 0.

For example, when running OPG with the input path below:

```bash
$ tree
ml_model/
├── folder1
│   └── file11
├── folder2
├── folder4
│   ├── folder41
│   │   └── file411
│   └── file42
├── file1
├── file2
```

The ouput MUST contain the folowing (JSON-represented) object:

```json
[
    {
        "path": "file1",
        "type": "file",
        "size": 25,
    },
    {
        "path": "file2",
        "type": "file",
        "size": 100,
    },
    {
        "path": "folder1/file11",
        "type": "file",
        "size": 10,
    },
    {
        "path": "folder2",
        "type": "dir",
        "size": 0,
    },
    {
        "path": "folder4/file42",
        "type": "file",
        "size": 512,
    },
    {
        "path": "folder4/folder41/file411",
        "type": "file",
        "size": 612,
    },
]
```

### Hashing Task Generation (HTG)

The HTG sub-routine takes as input the output of the OPG sub-routine and a shard size, and outputs a list of independent "hashing tasks".
A "hashing task" is a request to hash a portion (shard) of a file. Each hashing task can be run independently of one another, in parallel.

```java
hashing_task struct {
    path_metadata   // Path metadata from OFL output.
    offset_start    // The position of the first byte to hash.
    offset_end      // The position of the last byte to hash.
}

func HTG([]path_metadata, shard_size) -> hashing_task
```

The shard size is used to partition each file content into multiple hashing tasks. Each task represents the hashing of a portion / shard of a file.
So `task_i` hashes a file content from offset `i*shard_size` to offset `MIN( (i+1)*shard_size - 1, file_size - 1 )`.

Example 1: A file of size 20 bytes and using a shard of size 10 bytes. Two tasks are generated:

- `task_0` is for hashing the file from offset `0` to offset `9` (10 bytes)
- `task_1` is for hashing the file from offset `10` to offset `19` (10 bytes)

Example 2: A file of size 20 bytes and using a shard of size 6 bytes. Four tasks are generated:

- `task_0` is for hashing the file from offset `0` to offset `5` (6 bytes)
- `task_1` is for hashing the file from offset `6` to offset `11` (6 bytes)
- `task_2` is for hashing the file from offset `12` to offset `17` (6 bytes)
- `task_3` is for hashing the file from offset `18` to offset `19` (2 bytes)

### Hashing Task Execution (HTE)

The HTE sub-routine takes as input a model path type and a list of hashing tasks. It performs the actual hashing and returns a digest:

```java
func HTE(model_path_type, hashing_task) -> digest
```

The HTE routine performs the following logic:

1. If the `model_path_type` is a directory:
    1. Compute the temporary value `TYPE_STR := UTF-8( hashing_task.path_metadata.type )`
    1. Compute the temporary value `PATH_STR := BASE64( UTF-8( hashing_task.path_metadata.path ) )`
    1. Compute the temporary value `POS_STR := UTF-8( ITOA(start_pos) + "-" + ITOA(end_pos) )`
    1. Compute the header as `HEADER := TYPE_STR + "." + PATH_STR + "." + POS_STR`
    1. If `hashing_task.path_metadata.type == "dir"` (an empty directory), output `SHA256( HEADER + "." + "none" )`. Else continue.
    1. (A non-empty directory), output `SHA256( HEADER + "." + READ_FILE(hashing_task.path_metadata.path, start=hashing_task.offset_start, end=hashing_task.offset_end) )`.

1. If the model is a single file:
    1. Compute the temporary value `TYPE_STR := UTF-8( hashing_task.path_metadata.type )`
    1. Compute the temporary value `PATH_STR := BASE64( "root" )`
    1. Compute the temporary value `POS_STR := UTF-8( ITOA(start_pos) + "-" + ITOA(end_pos) )`
    1. Compute the header as `HEADER := TYPE_STR + "." + PATH_STR + "." + POS_STR`
    1. Output `SHA256( HEADER + "." + READ_FILE(hashing_task.path_metadata.path, start=hashing_task.offset_start, end=hashing_task.offset_end) )`

### Final Digest Computation (FDC)

The FDC sub-routine takes as input a list of digests output by the PCMH routine, and outputs a single digest. The FDC simply performs concatenation of the list of input digests and hashes them:

```java
func FDC(digests []digest) -> digest
    return CONCAT( digests[0], digests[1], ..., digests[i], ... digests[len(digests) - 1] )
```

### High-level Implementation

```java
func DIRSHA256(model_path, shard_size) -> digest
    ordered_paths_metadata := OPG(model_path)
    hashing_tasks := HTG(ordered_paths_metadata, shard_size)
    digests := []
    for i := range hashing_tasks
        task_i := hashing_tasks[i]
        digest_i := HTE(model_path.type(), task_i)
        digests += [digest_i]
    return FDC(digests)
```

### Test Vectors

See function `test_known_file()` in [serialize_test.py](https://github.com/google/model-transparency/blob/main/model_signing/serialize_test.py#L416) and `test_known_folder()` of [seralize_tests.py](https://github.com/google/model-transparency/blob/main/model_signing/serialize_test.py#L558)

## DIRSHA256-p1

DIRSHA256 is the instantiation of DIRSHA256 with a pre-defined, fixed shard size of 1GB. This set of parameters is referred to as `p1`.

### Test Vectors

TODO

## Sample code

You can find a reference implementation in the function `serialize_v1()` of [serialize.py](https://github.com/google/model-transparency/blob/main/model_signing/serialize.py#L325).
