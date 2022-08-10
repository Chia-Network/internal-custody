from pathlib import Path

from clvm_tools_rs import compile_clvm
from unittest import TestCase

from chia.types.blockchain_format.program import Program, SerializedProgram

program_files = set(
    [
        "cic/clsp/singleton/prefarm_inner.clsp",
        "cic/clsp/singleton/singleton_top_layer_v1_1.clsp",
        "cic/clsp/drop_coins/ach_clawback.clsp",
        "cic/clsp/drop_coins/ach_completion.clsp",
        "cic/clsp/drop_coins/p2_merkle_tree.clsp",
        "cic/clsp/drop_coins/p2_new_lock_level.clsp",
        "cic/clsp/drop_coins/rekey_clawback.clsp",
        "cic/clsp/drop_coins/rekey_completion.clsp",
        "cic/clsp/filters/only_rekey.clsp",
        "cic/clsp/filters/rekey_and_payment.clsp",
    ]
)

clvm_include_files = set(
    [
        "cic/clsp/include/condition_codes.clib",
        "cic/clsp/include/curry_and_treehash.clib",
        "cic/clsp/include/merkle_utils.clib",
        "cic/clsp/include/singleton_truths.clib",
        "cic/clsp/include/utility_macros.clib",
    ]
)

CLVM_PROGRAM_ROOT = "cic/clsp"


def list_files(dir, glob):
    dir = Path(dir)
    entries = dir.rglob(glob)
    files = [f for f in entries if f.is_file()]
    return files


def read_file(path):
    with open(path) as f:
        return f.read()


def path_with_ext(path, ext):
    return Path(str(path) + ext)


class TestClvmCompilation(TestCase):
    """
    These are tests, and not just build scripts to regenerate the bytecode, because
    the developer must be aware if the compiled output changes, for any reason.
    """

    def test_all_programs_listed(self):
        """
        Checks to see if a new .clsp file was added to chia/wallet/puzzles, but not added to `program_files`
        """
        existing_files = list_files(CLVM_PROGRAM_ROOT, "*.cl[si][pb]")
        existing_file_paths = set([Path(x).relative_to(CLVM_PROGRAM_ROOT) for x in existing_files])

        expected_files = set(clvm_include_files).union(program_files)
        expected_file_paths = set([Path(x).relative_to(CLVM_PROGRAM_ROOT) for x in expected_files])

        self.assertEqual(
            expected_file_paths,
            existing_file_paths,
            msg="Please add your new program to `program_files` or `clvm_include_files.values`",
        )

    def test_include_and_source_files_separate(self):
        self.assertEqual(clvm_include_files.intersection(program_files), set())

    # TODO: Test recompilation with all available compiler configurations & implementations
    def test_all_programs_are_compiled(self):
        """Checks to see if a new .clsp file was added without its .hex file"""
        all_compiled = True
        msg = "Please compile your program with:\n"

        # Note that we cannot test all existing .clsp files - some are not
        # meant to be run as a "module" with load_clvm; some are include files
        # We test for inclusion in `test_all_programs_listed`
        for prog_path in program_files:
            try:
                output_path = path_with_ext(prog_path, ".hex")
                hex = output_path.read_text()
                self.assertTrue(len(hex) > 0)
            except Exception as ex:
                all_compiled = False
                msg += f"    run -i {prog_path.parent} -d {prog_path} > {prog_path}.hex\n"
                print(ex)
        msg += "and check it in"
        self.assertTrue(all_compiled, msg=msg)

    def test_recompilation_matches(self):
        self.maxDiff = None
        unique_search_paths = []
        for path in clvm_include_files:
            search_dir = Path(path).parent
            if search_dir not in unique_search_paths:
                unique_search_paths.append(search_dir)
        for f in program_files:
            f = Path(f)
            compile_clvm(
                str(f),
                str(path_with_ext(f, ".recompiled")),
                search_paths=[str(p) for p in [f.parent, *unique_search_paths]],
            )
            orig_hex = path_with_ext(f, ".hex").read_text().strip()
            new_hex = path_with_ext(f, ".recompiled").read_text().strip()
            self.assertEqual(orig_hex, new_hex, msg=f"Compilation of {f} does not match {f}.hex")
        pass

    def test_all_compiled_programs_are_hashed(self):
        """Checks to see if a .hex file is missing its .sha256tree file"""
        all_hashed = True
        msg = "Please hash your program with:\n"
        for prog_path in program_files:
            try:
                hex = path_with_ext(prog_path, ".hex.sha256tree").read_text()
                self.assertTrue(len(hex) > 0)
            except Exception as ex:
                print(ex)
                all_hashed = False
                msg += f"    opd -H {prog_path}.hex | head -1 > {prog_path}.hex.sha256tree\n"
        msg += "and check it in"
        self.assertTrue(all_hashed, msg)

    # TODO: Test all available shatree implementations on all progams
    def test_shatrees_match(self):
        """Checks to see that all .sha256tree files match their .hex files"""
        for prog_path in program_files:
            # load the .hex file as a program
            hex_filename = path_with_ext(prog_path, ".hex")
            clvm_hex = hex_filename.read_text()  # .decode("utf8")
            clvm_blob = bytes.fromhex(clvm_hex)
            s = SerializedProgram.from_bytes(clvm_blob)
            p = Program.from_bytes(clvm_blob)

            # load the checked-in shatree
            existing_sha = path_with_ext(prog_path, ".hex.sha256tree").read_text().strip()

            self.assertEqual(
                s.get_tree_hash().hex(),
                existing_sha,
                msg=f"Checked-in shatree hash file does not match shatree hash of loaded SerializedProgram: {prog_path}",  # noqa
            )
            self.assertEqual(
                p.get_tree_hash().hex(),
                existing_sha,
                msg=f"Checked-in shatree hash file does not match shatree hash of loaded Program: {prog_path}",
            )
