# riscv-arch-test on the DUT

`python3 verif/arch-test/run_arch.py` runs the RV32E tests of riscv-arch-test 3.9.1
(`third_party/riscv-arch-test`, a submodule pinned at tag 3.9.1, commit eb66181dd) on the design in
`src/`. It writes `build/arch.json` as `{"pass": bool, "tests": {name: bool}}` and exits 0 only if
every test passes. `build/arch/report.json` holds the errors, the skips, the split tests and the
excluded cases.

Options: `--src DIR` (another design), `--seeds 1,2,3` (the default), `--jobs N`,
`--only NAME ...` (writes `build/arch/only.json` instead, so a partial run never leaves a passing
`build/arch.json`).

## What one test does

1. Build one ELF with `riscv32-unknown-elf-gcc -march=rv32e -mabi=ilp32e -mno-relax`, the defines
   riscof would pass (`XLEN=32`, `RVTEST_E=True`, `TEST_CASE_1=True`) and
   `verif/arch-test/model_test.h`, linked at 0x10000 with `verif/programs/link.ld`. The ELF is the
   same for spike and the DUT; `verif/programs/elf2hex.py --base 0x10000` turns it into the 64 KB
   RAM image.
2. Reference signature: `spike --isa=rv32e --misaligned --disable-dtb -m0x1000:0x1f000
   +signature=... +signature-granularity=4`, cached in `build/arch/ref/<name>.sig`, keyed on the
   ELF's bytes and the spike binary.
3. The reference ISS (`build/cosim/iss_main --check-init`) must run the image to EBREAK without an
   unspecified instruction, a read of an unwritten register or an access to the core region
   (contract v2 section 3), and leave the same signature as spike. This cross-checks the ISS and
   the image before the DUT is involved.
4. The DUT runs the image through `run_cosim.run_one("pins", ..., dump_ram=...)`, which is what
   `run_cosim.py --mode pins --dump-ram` does, once per seed. The cosim must pass (its program SPI
   writes, uo_out and final RAM outside the core region equal the ISS's), and the signature words
   in the dumped RAM must equal spike's.

**Contract v2 (main 35b92b5): the core region.** RAM 0xFF80-0xFFFF, in every 64 KB alias, may
hold the core's registers, pc and scratch, so no test code, data or signature may land there.
`link.ld` puts code and data below 0xF000 (its `ASSERT`) and the stack top at 0xFF80; the tests
use no stack. `run_arch.py` checks that the signature lies inside 0x10000-0x1FF7F (the
relocated 0x0000-0xFF7F), and the ISS run fails on any access to the region. The DUT's region
contents are its own: the cosim starts them random, never compares them, and the signature
compare reads only signature words.

## The model (`verif/arch-test/model_test.h`)

- **Boot.** spike starts at its reset vector 0x1000, where `RVMODEL_BOOT` places a jump to the
  test. The DUT starts at pc 0, which is image offset 0: the test's first instruction (fetch uses
  pc[15:0]). `RVMODEL_BOOT` zeroes x1..x15 (the DUT's registers power up unknown, and
  `RVTEST_INIT_GPRS` leaves x14 and x15 to the test), then jumps to its own linked address. From
  there the DUT runs at 0x1xxxx like spike, so AUIPC and JAL results and data addresses agree.
- **Halt.** `RVMODEL_HALT` writes 1 to `tohost` (spike's HTIF exit) and executes EBREAK (the DUT's
  halt, contract section 4).
- **Signature.** `begin_signature` and `end_signature` around the test's signature area (canaries
  included); `tohost` and `fromhost` sit just before it.
- No CSRs and no trap handler: `rvtest_mtrap_routine` is never defined, so `arch_test.h` emits no
  trap prolog, handler or CSR access.

## Which tests run

All 37 tests of `rv32e_m/E` and 6 of `rv32e_m/privilege`: 43 tests.

| Test | Note |
|---|---|
| `add-01` .. `xori-01` (37 tests, `rv32e_m/E`) | all of RV32E's computational, branch, jump, load and store instructions |
| `misalign-lh-01`, `misalign-lhu-01`, `misalign-lw-01`, `misalign-sh-01`, `misalign-sw-01` | misaligned data accesses, which never trap here (contract section 3), built as RVTEST_CASE 0 (hardware misaligned support, no trap handler). `misalign-lhu-01`'s case 0 also lists `rvtest_mtrap_routine`; it isn't defined, since a misaligned access never traps here and the handler would need CSRs. |
| `misalign1-jalr-01` | JALR whose rs1 + imm has bit 0 set (cleared by JALR; no trap) |

### Split tests

The branch tests carry 200 to 280 KB of code, mostly NOP padding that sets each case's branch
distance, and `jal-01` 14 MB, so they don't fit the 64 KB RAM as one image. `run_arch.py`
splits a test whose cases add up to more than 44 KB (0xB000 bytes) into pieces at its `inst_N:`
case boundaries. A piece is the whole test minus the other pieces' cases; every `RVTEST_SIGBASE`
line stays in place. The cases are independent (each loads its own operands and stores at an
explicit offset below 2048 from the current signature base, which only `RVTEST_SIGBASE` sets), so
each case runs exactly once, in one piece, on spike, the ISS and the DUT alike. A test passes when
all its pieces pass.

| Test | Pieces |
|---|---|
| `beq-01` | 6 |
| `bne-01` | 6 |
| `blt-01` | 6 |
| `bge-01` | 5 |
| `bltu-01` | 6 |
| `bgeu-01` | 7 |

### Excluded cases (jal-01)

15 of `jal-01`'s 16 cases jump 512 KB (`inst_1`, `inst_2`) or 1 MB (`inst_3` .. `inst_15`): each
case is 524,544 to 1,048,884 bytes of code. In a 64 KB memory with fetch at pc[15:0], a jump by a
multiple of 64 KB lands on the jump itself, so these cases can't run on this core at all. They are
excluded, listed per case in `build/arch/report.json`; `inst_0` (a 4-byte JAL) runs. J immediates
with bits 16..20 set (the sign included) are covered instead by `verif/programs/selftest.S`
section 7: a JAL by +0xF0010 and one by -0xFFFF0, whose targets alias to code a few bytes ahead
while AUIPC sees the full 32-bit pc.

This exclusion is the lead's ruling R10, and `run_arch.py` locks it: `EXCLUDED = {"jal-01":
inst_1..inst_15}`. A test whose planned exclusions differ from that set (one case more or fewer, or
an exclusion in any other test), or that is left with no runnable piece, fails, and so does the
whole run when the union of the exclusions differs from the ruled set. A suite update that changes
which cases don't fit must come back to the lead, not shrink the suite silently. The reference
ISS's RAM dump is deleted before each ISS run, so a stale dump can't stand in for a crashed run.

## Skipped tests

Only tests that need CSRs or a trap handler are skipped:

| Test | Reason |
|---|---|
| `ecall` | ECALL is a fatal trap here (contract section 4); the test needs a trap handler (mtvec, mcause, mepc: CSRs), which the core doesn't have |
| `ebreak` | EBREAK is the halt here (contract section 4); the test needs a trap handler (CSRs) |
| `misalign-beq-01`, `misalign-bne-01`, `misalign-blt-01`, `misalign-bge-01`, `misalign-bltu-01`, `misalign-bgeu-01` | a taken branch to a target with bit 1 set is a fatal trap here; the test needs the C extension (case 0) or a trap handler with CSRs (case 1) |
| `misalign-jal-01` | the same for JAL |
| `misalign2-jalr-01` | the same for JALR |

Not part of RV32E, so not run: `rv32e_m/M`, `rv32e_m/C`, `rv32e_m/B` and `rv32e_m/Zifencei`
(FENCE.I is reserved here, contract section 4).

## Result on the seed core

`python3 verif/arch-test/run_arch.py` on the seed (src/ as merged from 97153ff): 43 of 43 tests
pass with seeds 1, 2 and 3, 10 skipped, 6 split, 15 cases of `jal-01` excluded; 2.97 s wall clock
(12.3 s user) with the Verilator model already built.

With the contract v2 checkers, the seed and both real v2 designs pass the same 43 tests with the
same skips, splits and exclusions: `run_arch.py --src verif/cosim/mutants/legal_h002_rf`
(hyp/002-rf-in-ram fc07cc3) and `--src verif/cosim/mutants/legal_h002_pcram`
(hyp/002-rf-in-ram-pcram b9a5211).
