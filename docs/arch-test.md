# riscv-arch-test on the DUT

Two contracts until the integration swap (plan Task 9): contract v2 (the default, this section
and the ones up to "Result on the seed core") and contract v3 with `--v3` (section
[Contract v3](#contract-v3---v3) at the end).

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

## Contract v3 (`--v3`)

`python3 verif/arch-test/run_arch.py --v3` runs the same suite under `docs/contract.md`: code
in the QSPI Pmod's flash, data and the signature in its PSRAM, traps to `mtvec`. It writes
`build/arch.json` and `build/arch/report.json` in the same formats (the report adds
`"contract": "v3"`, the ISA string, the suite commit, the spike ISA form and GCC version prep
used, and each run's cycles).

Options: `--src DIR`, `--seeds` (default `1,2,3,4`: `pmod_init_for_seed` maps them to the four
memory power-up modes, contract 2.4), `--jobs N`, `--only NAME ...` (writes `build/arch/only.json`
instead), and `--phase`:
- `prep`: steps 1-3 below. Needs `riscv32-unknown-elf-gcc` (or `CROSS=`), spike and a C++
  compiler (for `build/cosim/iss_main`). It stages the flash images, the reference signatures and
  `manifest.json` in `build/programs/arch-v3/`, where `tools/hwe_run.sh` mirrors them.
- `dut`: step 4 from the manifest. Needs Verilator only: no GCC, spike or `third_party/`. Before
  any cosim it refuses:
  - a manifest made by another flow (a hash of `model_test.h`, `link.ld`, `elf2hex.py` and the v3
    settings in `run_arch.py`, the same in any checkout);
  - where the suite is checked out, a manifest made from other suite content (a hash of the suite's
    `env/` headers and test sources; it works in `tools/hwe_run.sh` mirrors, which carry no
    `.git`), or one whose test list differs from the suite's;
  - a partial manifest (prep with `--only`) for a full run.
  Then each test refuses an image whose sha256 differs from the manifest's. The GCC version is
  recorded, not checked: the staged images and their references stay consistent with each other
  whatever compiler made them.
- `all` (the default): both, on a machine that has every tool.

`build/arch.json` is removed at the start of every phase, so `prep` alone never leaves one.

On hwe-bench, which has spike, dtc, the xPack GCC and Verilator on `tools/hwe_run.sh`'s PATH (main
11890e5 on), one host runs it all; `--third-party` mirrors the suite:

```
tools/hwe_run.sh --third-party --pull build/arch.json --pull build/arch/report.json -- \
    python3 verif/arch-test/run_arch.py --v3 --src SRC --jobs 16
```

Split, with prep on the Mac (GCC and spike, no Verilator by policy) and the cosim on hwe-bench
(no `--third-party` needed):

```
python3 verif/arch-test/run_arch.py --v3 --phase prep --jobs 4                # the Mac
tools/hwe_run.sh --pull build/arch.json --pull build/arch/report.json -- \
    python3 verif/arch-test/run_arch.py --v3 --phase dut --src SRC --jobs 16  # hwe-bench
```

### What one test does (v3)

1. **Build** one ELF with `-march=rv32e_zicsr -mabi=ilp32e -mno-relax`, `-DXLEN=32 -DBONETTO_V3`,
   and the `def`s of the test's one `RVTEST_CASE` whose conditions hold for the model (ISA
   `RV32EZicsr`, hardware misaligned data access: riscof's selection; `run_arch.py` fails a test
   with zero or several matching cases, or a condition it doesn't know). The linker scripts are
   `verif/programs/link.ld` with `.text` moved to 0x1000 (`--section-start`), plus `SIG_LD` in
   `run_arch.py`, which adds the `.signature` section through `INSERT AFTER .window`.
2. **Reference:** `spike --priv=m --disable-dtb -m0x1000:0xfff000,0x1000000:0x800000,0x80000000:0x1000
   --instructions=50000000 --isa=rv32e_zicsr_zicclsm +signature=... +signature-granularity=4`,
   cached in `build/arch/ref-v3/`, keyed on the ELF, the spike arguments and the spike binary.
   - Zicclsm gives hardware misaligned loads and stores (contract 3). It is spike's only way since
     riscv-isa-sim d6a5e90c (Dec 2025) removed `--misaligned`.
   - A spike that rejects Zicclsm (Homebrew's 1.1.1-dev on the Mac does) gets
     `--isa=rv32e_zicsr --misaligned` instead. `run_arch.py` probes this once per run and records
     the form in the manifest.
   - Both forms give identical signatures for all 53 tests (Mac spike `--misaligned` against
     hwe-bench spike Zicclsm, and both forms on hwe-bench's spike, 2026-10-17).
   - spike exits 1 if a trap the test doesn't expect reaches `rvmodel_trap`.
3. **Reference ISS:** `build/cosim/iss_main --v3 --check-init --dump-psram` must reach the
   program end (the self-loop) with `GPIO_OUT = 0x5A`, without an unspecified step, a read of an
   unwritten register or arbitrary-reset CSR, or a core-region access, and its PSRAM must hold
   spike's signature.
4. **DUT:** `run_cosim.run_one("pins", ..., v3=True, dump_ram=...)` per seed, which runs the v3
   harness with `--dump-psram`. The program is deterministic (no volatile read, interrupts never
   enabled), so the harness judges it against its own ISS run: uo_out goes through the ISS's GPIO
   states (0, then 0x5A), every PSRAM write outside the core region writes a byte of a store, and
   the final PSRAM bytes the program wrote match. Then the signature words in the dumped PSRAM must
   equal spike's.

### Memory layout and the model (`model_test.h` with `-DBONETTO_V3`)

| Where | What |
|---|---|
| flash 0x0000_0000 | `jal x0, 0x1000` (0x0000106F): in the DUT image only |
| flash 0x0000_1000 | `.text`: `rvtest_entry_point` first (checked), the test, arch_test.h's trap handler |
| flash after the code | the load images of `.data` and `.signature` |
| PSRAM 0x0100_0000 | `.data`: arch_test.h's trap save areas, the test's input data, `tohost` |
| PSRAM 0x0101_0000 | `.signature`: `begin_signature` .. `end_signature` |
| 0x8000_0000 | GPIO_OUT (on spike: a scratch page) |

- **Start.** spike's debug module owns [0, 0x1000) and, with `--disable-dtb`, spike starts at its
  reset vector 0x1000. So the ELF starts there, and the DUT image adds one word at flash byte 0:
  the DUT starts at pc 0, jumps to 0x1000, and from there both run the same code at the same
  addresses. The ISS runs the DUT image, stub included.
- **Boot** (`RVMODEL_BOOT`, before arch_test.h's trap prolog): copy `.data` and `.signature` from
  flash (PSRAM starts arbitrary, contract 3); write `mtvec` (`rvmodel_trap`), `mscratch` and
  `mstatus` (their reset values are arbitrary and the prolog reads the first two); zero x1..x15.
- **Halt** (`RVMODEL_HALT`): `tohost = 1` (spike's HTIF exit), `GPIO_OUT = 0x5A`, then `jal x0, 0`,
  which retires twice at the same pc: the program end (contract 4). The GPIO write lets the
  pins-mode harness see the end at the pins, so a run stops soon after it instead of at the
  worst-case bound.
- **Unexpected trap:** `rvmodel_trap` writes `tohost = 3` and `GPIO_OUT = 0xEE` and self-loops.
- **mtval** reads 0 (contract 4), so `model_test.h` sets `SET_REL_TVAL_MSK` to 0: arch_test.h's
  handler then stores 0 as each trap's tval signature word, on spike and the DUT alike. It still
  stores the trap entry word, `mcause` and `mepc` relative to the code start. So the arch tests
  mask mtval's value: a DUT whose mtval read something else would still pass them. Cosim covers it
  instead: the `csr` program checks that mtval reads 0 and ignores writes, and the rvfi-mode cosim
  compares every CSR read with the ISS.

### Which tests run (v3)

All 53 tests: the 37 of `rv32e_m/E` and the 16 of `rv32e_m/privilege`. Nothing is skipped
(`SKIP_V3` is empty), split or excluded.

| Tests | Case | Note |
|---|---|---|
| `add-01` .. `xori-01` (37, `rv32e_m/E`) | 0 | as in v2 |
| `ecall`, `ebreak` | 1 | trap to `mtvec`, `mcause` 11 and 3, through arch_test.h's handler |
| `misalign-{beq,bne,blt,bge,bltu,bgeu}-01`, `misalign-jal-01`, `misalign2-jalr-01` | 1 (no C, Zicsr) | a taken jump to a target with bit 1 set traps with `mcause` 0 (contract 4) |
| `misalign-{lh,lw,sh,sw}-01` | 0 (hardware misaligned) | misaligned loads and stores never trap (contract 3) |
| `misalign-lhu-01` | 0 | the same; its case 0 also defines `rvtest_mtrap_routine`, so the handler's prolog and epilog run |
| `misalign1-jalr-01` | 0 | JALR clears bit 0 of the target; no trap |

Every test with `rvtest_mtrap_routine` also takes the `ecall` with x2 = 0 that arch_test.h's
`RVTEST_CODE_END` uses to return to M-mode. The handler uses only contract-4 CSRs: `mtvec`,
`mscratch`, `mepc`, `mcause`, `mtval`, `misa` and `mstatus`.

**Versus v2:** the 10 trap tests that v2 skipped now run (contract v3 has the CSRs and traps to
`mtvec`). The branch tests and `jal-01` now run whole: the flash holds 16 MiB and nothing aliases
inside it, so v2's 64 KB splitting and ruling R10's exclusion of `jal-01` `inst_1` .. `inst_15`
(its 512 KB and 1 MB jumps) don't apply. `jal-01`'s image is 14 MB. Ruling R10 still governs the
v2 flow.

### Result on the seed (v3)

Measured 2026-10-17. Times are wall clock; cycles are the harness's `cycles` (reset release to
the end marker).
- **Split flow** (setup/v3-arch 440121b, seed `origin/setup/v3-seed` 5879b91 `src/`): 53 of 53
  tests pass under seeds 1, 2, 3 and 4.
  - `--phase prep` took 3.3 s on the Mac (`--jobs 4`, spike cache cleared).
  - `--phase dut --jobs 16` took 58 s on hwe-bench, the Verilator model already built.
- **Cycles:** a run takes 4,570,994 (`misalign-sh-01`) to 12,399,419 (`bltu-01`) cycles. Each
  test's count is the same under every seed. Most of a short test is `RVMODEL_BOOT`'s copy of the
  4 KiB `.data` section.
- **One host** (setup/v3-arch b79ee0c, seed `origin/setup/v3-seed` 133d76b `src/`): `--phase all
  --jobs 16` on hwe-bench passes 53 of 53 under seeds 1-4 with spike in the Zicclsm form, with the
  same cycles as above.
  - The `tools/hwe_run.sh --third-party` call took 3 min 27 s in total. That includes the first
    mirror of `third_party/` (767 MB) and the Verilator build.
  - Its images are byte-identical to the Mac's (53 of 53 sha256 equal: the xPack GCC 15.2.0 arm64
    and x86_64 builds), and so are its references.

A mutant of the seed whose trap entry writes `mcause ^ 1` (seed 1) fails `ecall`, `ebreak`,
`misalign-beq-01`, `misalign-jal-01`, `misalign2-jalr-01` and `misalign-lhu-01` (its final
`ecall`) and passes `add-01`.
