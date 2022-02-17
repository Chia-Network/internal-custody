import importlib
import inspect
import os
import pathlib
import pkg_resources

from typing import List

from clvm_tools.clvmc import compile_clvm as compile_clvm_py
from chia.types.blockchain_format.program import Program, SerializedProgram

compile_clvm = compile_clvm_py

# Handle optional use of clvm_tools_rs if available and requested
if "CLVM_TOOLS_RS" in os.environ:
    try:

        def sha256file(f):
            import hashlib

            m = hashlib.sha256()
            m.update(open(f).read().encode("utf8"))
            return m.hexdigest()

        from clvm_tools_rs import compile_clvm as compile_clvm_rs

        def translate_path(p_):
            p = str(p_)
            if os.path.isdir(p):
                return p
            else:
                module_object = importlib.import_module(p)
                return os.path.dirname(inspect.getfile(module_object))

        def rust_compile_clvm(full_path, output, include_paths=[]):
            treated_include_paths = list(map(translate_path, include_paths))
            compile_clvm_rs(str(full_path), str(output), treated_include_paths)

            if os.environ["CLVM_TOOLS_RS"] == "check":
                orig = str(output) + ".orig"
                compile_clvm_py(full_path, orig, include_paths=include_paths)
                orig256 = sha256file(orig)
                rs256 = sha256file(output)

                if orig256 != rs256:
                    print("Compiled %s: %s vs %s\n" % (full_path, orig256, rs256))
                    print("Aborting compilation due to mismatch with rust")
                    assert orig256 == rs256

        compile_clvm = rust_compile_clvm
    finally:
        pass


def make_include_paths_relative(base_path: pathlib.Path, include_paths: List[pathlib.Path]) -> List[pathlib.Path]:
    return [p.resolve() if pathlib.Path(p).is_absolute() else base_path.joinpath(p).resolve() for p in include_paths]


def load_serialized_clvm(clvm_filename, package_or_requirement=__name__, include_paths=[]) -> SerializedProgram:
    """
    This function takes a .clvm file in the given package and compiles it to a
    .clvm.hex file if the .hex file is missing or older than the .clvm file, then
    returns the contents of the .hex file as a `Program`.

    clvm_filename: file name
    package_or_requirement: usually `__name__` if the clvm file is in the same package
    """

    hex_filename = f"{clvm_filename}.hex"

    try:
        if pkg_resources.resource_exists(package_or_requirement, clvm_filename):
            full_path = pathlib.Path(pkg_resources.resource_filename(package_or_requirement, clvm_filename))
            base_path = full_path.parent
            output = base_path / hex_filename
            # copy search paths so we can morph it
            include_paths = list(include_paths)
            include_paths.append(base_path)
            include_paths = make_include_paths_relative(base_path, include_paths)
            compile_clvm(full_path, output, search_paths=include_paths)

    except NotImplementedError:
        # pyinstaller doesn't support `pkg_resources.resource_exists`
        # so we just fall through to loading the hex clvm
        pass

    clvm_hex = pkg_resources.resource_string(package_or_requirement, hex_filename).decode("utf8")
    clvm_blob = bytes.fromhex(clvm_hex)
    return SerializedProgram.from_bytes(clvm_blob)


def load_clvm(clvm_filename, package_or_requirement=__name__, include_paths=[]) -> Program:
    return Program.from_bytes(
        bytes(
            load_serialized_clvm(
                clvm_filename, package_or_requirement=package_or_requirement, include_paths=include_paths
            )
        )
    )
