# Equivalence of the peripheral split (hypothesis 114)

`run.ys` proves that `src/r32v_periph.v` with its five peripheral modules (`r32v_gpio`,
`r32v_timer`, `r32v_pwm`, `r32v_uart`, `r32v_spi`) is sequentially equivalent to the one-module
`r32v_periph.v` it replaced (f4fb72c). Put the old file in `old/` and the six new files in `new/`,
then run `yosys -s run.ys`: both sides are flattened, the submodules' internal registers are
renamed back to their old names so that every register is matched, and `equiv_simple -seq 8`,
`equiv_induct -seq 8` and `equiv_status -assert` must prove every matched signal.

Result (hwe-bench, 2026-10-20): 401 of 401 `$equiv` cells proven (125 by `equiv_simple`, 276 by
`equiv_induct`), the outputs `uo_out`, `rdata` and `irq` among them. A planted bug (the IR gate
`t_out | ~irg` changed to `t_out & ~irg` in `r32v_pwm.v`) leaves 1 cell unproven and fails.
