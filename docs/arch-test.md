# riscv-arch-test on the DUT

`python3 verif/arch-test/run_arch.py` runs the RV32E tests of riscv-arch-test 3.9.1
(`third_party/riscv-arch-test`, a submodule pinned at tag 3.9.1, commit eb66181dd) on the design in
`src/`, under `docs/contract.md` (contract v3): code in the QSPI Pmod's flash, data and the
signature in its PSRAM, traps to `mtvec`. It writes `build/arch.json` as `{"pass": bool, "tests":
{name: bool}}` and exits 0 only if every test passes. `build/arch/report.json` holds the errors,
the skips, `"contract": "v3"`, the ISA string, the suite commit, the spike ISA form and GCC version
prep used, and each run's cycles.

`make arch` (`tools/eval.py --gate arch`) runs `run_arch.py --jobs N`, the one-host flow below,
and requires all 53 tests (ruling R34). The contract v2 flow (64 KB RAM images at 0x10000, split
branch tests, ruling R10's `jal-01` exclusion, the 10 skipped trap tests) was deleted at the
integration swap (plan Task 9, ruling R26); git history has it. `--v3`, which selected this flow
before the swap, is still accepted and does nothing.

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
11890e5 on), one host runs it all; `--third-party` mirrors the suite (check it out first:
`git submodule update --init third_party/riscv-arch-test`):

```
tools/hwe_run.sh --third-party --pull build/arch.json --pull build/arch/report.json -- \
    python3 verif/arch-test/run_arch.py --src SRC --jobs 16
tools/hwe_run.sh --third-party --pull build/gates/arch.json -- make arch     # the gate
```

Split, with prep on the Mac (GCC and spike, no Verilator by policy) and the cosim on hwe-bench
(no `--third-party` needed):

```
python3 verif/arch-test/run_arch.py --phase prep --jobs 4                # the Mac
tools/hwe_run.sh --pull build/arch.json --pull build/arch/report.json -- \
    python3 verif/arch-test/run_arch.py --phase dut --src SRC --jobs 16  # hwe-bench
```

## What one test does

1. **Build** one ELF with `-march=rv32e_zicsr -mabi=ilp32e -mno-relax`, `-DXLEN=32`,
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
3. **Reference ISS:** `build/cosim/iss_main --check-init --dump-psram` must reach the
   program end (the self-loop) with `GPIO_OUT = 0x5A`, without an unspecified step, a read of an
   unwritten register or arbitrary-reset CSR, or a core-region access, and its PSRAM must hold
   spike's signature.
4. **DUT:** `run_cosim.run_one("pins", ..., dump_ram=...)` per seed, which runs the cosim
   harness with `--dump-psram`. The program is deterministic (no volatile read, interrupts never
   enabled), so the harness judges it against its own ISS run: uo_out goes through the ISS's GPIO
   states (0, then 0x5A), every PSRAM write outside the core region writes a byte of a store, and
   the final PSRAM bytes the program wrote match. Then the signature words in the dumped PSRAM must
   equal spike's.

## Memory layout and the model (`verif/arch-test/model_test.h`)

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

## Which tests run

All 53 tests: the 37 of `rv32e_m/E` and the 16 of `rv32e_m/privilege`. Nothing is skipped
(`SKIP_V3` is empty), split or excluded.

| Tests | Case | Note |
|---|---|---|
| `add-01` .. `xori-01` (37, `rv32e_m/E`) | 0 | all of RV32E's computational, branch, jump, load and store instructions |
| `ecall`, `ebreak` | 1 | trap to `mtvec`, `mcause` 11 and 3, through arch_test.h's handler |
| `misalign-{beq,bne,blt,bge,bltu,bgeu}-01`, `misalign-jal-01`, `misalign2-jalr-01` | 1 (no C, Zicsr) | a taken jump to a target with bit 1 set traps with `mcause` 0 (contract 4) |
| `misalign-{lh,lw,sh,sw}-01` | 0 (hardware misaligned) | misaligned loads and stores never trap (contract 3) |
| `misalign-lhu-01` | 0 | the same; its case 0 also defines `rvtest_mtrap_routine`, so the handler's prolog and epilog run |
| `misalign1-jalr-01` | 0 | JALR clears bit 0 of the target; no trap |

Every test with `rvtest_mtrap_routine` also takes the `ecall` with x2 = 0 that arch_test.h's
`RVTEST_CODE_END` uses to return to M-mode. The handler uses only contract-4 CSRs: `mtvec`,
`mscratch`, `mepc`, `mcause`, `mtval`, `misa` and `mstatus`.

**Versus the deleted v2 flow:** the 10 trap tests that v2 skipped now run (contract v3 has the
CSRs and traps to `mtvec`). The branch tests and `jal-01` run whole: the flash holds 16 MiB and
nothing aliases inside it, so v2's 64 KB splitting and ruling R10's exclusion of `jal-01`
`inst_1` .. `inst_15` (its 512 KB and 1 MB jumps) don't apply. `jal-01`'s image is 14 MB.

Not part of RV32E, so not run: `rv32e_m/M`, `rv32e_m/C`, `rv32e_m/B` and `rv32e_m/Zifencei`
(FENCE.I is reserved, contract section 4).

## Result on the seed

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
