"""Eval-харнесс качества рисунков: прогон пула (tests/figure_pool.py) через
FORCED-figure путь на НАСТОЯЩЕМ Gemini + pdflatex. Меряет recall авто-триггера
`_task_needs_figure` и долю компилируемых рисунков по темам.

Запуск (нужны TeX Live + API-ключ → только в Docker):
    docker compose run --rm backend python scripts/eval_figures.py
"""
import asyncio

from tests.figure_pool import POOL
from app.ai.pipeline import _task_needs_figure, _solver_figure
from app.render.figures import compile_figure, FIG_RE

_ATTEMPTS = 3


async def _gen_and_compile(condition: str, topic: str) -> bool:
    err = ""
    for _ in range(_ATTEMPTS):
        try:
            fig = await _solver_figure(condition, "", "", err)
        except Exception as e:
            print(f"      gen error: {e}")
            return False
        m = FIG_RE.search(fig or "")
        if not m:
            err = "верни валидный %%FIG блок"
            continue
        png = await compile_figure(m.group(1))
        if png:
            return True
        err = "рисунок не скомпилировался — только числовые координаты"
    return False


async def main() -> None:
    rows = []
    for i, p in enumerate(POOL, 1):
        needs = _task_needs_figure(p["condition"], "", p["topic"])
        compiled = None
        if p["expects_figure"] and needs:
            compiled = await _gen_and_compile(p["condition"], p["topic"])
        mark = "OK" if (compiled is True) else ("—" if compiled is None else "FAIL")
        print(f"[{i:02d}] {p['topic']:11} expects={int(p['expects_figure'])} "
              f"needs={int(needs)} compiled={mark}  | {p['condition'][:55]}")
        rows.append((p["topic"], p["expects_figure"], needs, compiled))

    # ── Сводка ──
    print("\n================ СВОДКА ================")
    topics = sorted(set(r[0] for r in rows))
    for t in topics:
        tr = [r for r in rows if r[0] == t]
        fig_tasks = [r for r in tr if r[1]]
        recall = sum(1 for r in fig_tasks if r[2]) / max(1, len(fig_tasks))
        comp = [r for r in fig_tasks if r[2]]
        comp_rate = sum(1 for r in comp if r[3]) / max(1, len(comp))
        nonfig = [r for r in tr if not r[1]]
        false_trig = sum(1 for r in nonfig if r[2])
        print(f"{t:11}: триггер на figure-задачах {sum(1 for r in fig_tasks if r[2])}/{len(fig_tasks)} "
              f"(recall {recall:.0%}), компиляция {sum(1 for r in comp if r[3])}/{len(comp)} "
              f"({comp_rate:.0%}), ложных триггеров на non-figure {false_trig}/{len(nonfig)}")
    fig_all = [r for r in rows if r[1]]
    triggered = [r for r in fig_all if r[2]]
    print(f"\nИТОГО figure-задач: {len(fig_all)} | триггер {len(triggered)} "
          f"| собрался рисунок {sum(1 for r in triggered if r[3])}")
    nonfig_all = [r for r in rows if not r[1]]
    print(f"non-figure задач: {len(nonfig_all)} | ложных триггеров {sum(1 for r in nonfig_all if r[2])}")


if __name__ == "__main__":
    asyncio.run(main())
