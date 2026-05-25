# 04 limit — deepseek (`openrouter/deepseek/deepseek-chat-v3.1`)

- **Уровень / тема:** средний / matan
- **thinking:** False
- **input tokens:** 1930
- **output tokens:** 691
- **cost:** 0.141 ₽
- **time:** 26.23 s
- **RAG top_sim:** 0.780 (hits=5)

## Условие

Вычислите предел $\displaystyle\lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3}$.

## Эталонный ответ

$\dfrac{1}{2}$

## Ответ модели (raw)

```
\hd{Условие}
Вычислить предел: 
\[
\lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3}.
\]

\hd{Что найти}
Значение предела $\displaystyle L = \lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3}$.

\hd{Метод}
Используем разложение в ряд Тейлора (Маклорена) функций $\operatorname{tg} x$ и $\sin x$ в окрестности нуля до членов порядка $x^3$ включительно. Это стандартный метод вычисления пределов такого типа.

\hd{Решение}

\textbf{Шаг 1.} Выпишем известные разложения в окрестности $x = 0$:
\[
\sin x = x - \frac{x^3}{6} + o(x^3), \qquad
\operatorname{tg} x = x + \frac{x^3}{3} + o(x^3).
\]

Разложение $\operatorname{tg} x$ получается, например, из разложения $\tan x = \frac{\sin x}{\cos x}$ и $\cos x = 1 - \frac{x^2}{2} + o(x^3)$.

\textbf{Шаг 2.} Подставим эти разложения в числитель:
\begin{align*}
\operatorname{tg} x - \sin x &= \left[ x + \frac{x^3}{3} + o(x^3) \right] - \left[ x - \frac{x^3}{6} + o(x^3) \right] \\
&= x + \frac{x^3}{3} - x + \frac{x^3}{6} + o(x^3) \\
&= \frac{x^3}{3} + \frac{x^3}{6} + o(x^3).
\end{align*}

\textbf{Шаг 3.} Упростим сумму дробей:
\[
\frac{1}{3} + \frac{1}{6} = \frac{2+1}{6} = \frac{3}{6} = \frac{1}{2}.
\]
Следовательно,
\[
\operatorname{tg} x - \sin x = \frac{1}{2}\,x^3 + o(x^3).
\]

\textbf{Шаг 4.} Подставим полученное выражение в исходный предел:
\[
\frac{\operatorname{tg} x - \sin x}{x^3} = \frac{\frac{1}{2}x^3 + o(x^3)}{x^3} = \frac{1}{2} + \frac{o(x^3)}{x^3}.
\]

\textbf{Шаг 5.} Перейдем к пределу при $x \to 0$:
\[
\lim_{x\to 0} \left( \frac{1}{2} + \frac{o(x^3)}{x^3} \right) = \frac{1}{2} + 0 = \frac{1}{2}.
\]

\hd{Ответ}
\ans{ \displaystyle \frac{1}{2} }
```

## Оценка (заполняет Ярослав)

- Решено правильно? (да / нет / частично): 
- Нотация ВШЭ? (да / частично / нет): 
- Читаемо студенту? (да / нет): 
