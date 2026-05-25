# 04 limit — deepseek-r1 (`openrouter/deepseek/deepseek-r1-0528`)

- **Уровень / тема:** средний / matan
- **thinking:** False
- **input tokens:** 1931
- **output tokens:** 1859
- **cost:** 0.459 ₽
- **time:** 64.05 s
- **RAG top_sim:** 0.780 (hits=5)

## Условие

Вычислите предел $\displaystyle\lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3}$.

## Эталонный ответ

$\dfrac{1}{2}$

## Ответ модели (raw)

```
\hd{Условие}
Вычислить предел: \(\displaystyle \lim_{x \to 0} \frac{\operatorname{tg} x - \sin x}{x^3}\).

\hd{Что найти}
Значение предела \(\displaystyle \lim_{x \to 0} \frac{\operatorname{tg} x - \sin x}{x^3}\).

\hd{Метод}
Используем тригонометрические преобразования и известные пределы: \(\lim_{x \to 0} \frac{\sin x}{x} = 1\) и \(\lim_{x \to 0} \frac{1 - \cos x}{x^2} = \frac{1}{2}\).

\hd{Решение}

\textbf{Шаг 1.} Преобразуем числитель, выразив тангенс через синус и косинус:
\[
\operatorname{tg} x - \sin x = \frac{\sin x}{\cos x} - \sin x = \sin x \left( \frac{1}{\cos x} - 1 \right) = \sin x \cdot \frac{1 - \cos x}{\cos x}.
\]
Подставляем в исходный предел:
\[
\lim_{x \to 0} \frac{\sin x \cdot \frac{1 - \cos x}{\cos x}}{x^3} = \lim_{x \to 0} \frac{\sin x (1 - \cos x)}{x^3 \cos x}.
\]

\textbf{Шаг 2.} Разделим предел на произведение трёх пределов:
\[
\lim_{x \to 0} \left( \frac{\sin x}{x} \cdot \frac{1 - \cos x}{x^2} \cdot \frac{1}{\cos x} \right).
\]

\textbf{Шаг 3.} Вычислим каждый предел отдельно:
\begin{align*}
& \lim_{x \to 0} \frac{\sin x}{x} = 1, \\
& \lim_{x \to 0} \frac{1 - \cos x}{x^2} = \lim_{x \to 0} \frac{2 \sin^2 \frac{x}{2}}{x^2} = \lim_{x \to 0} \left( 2 \cdot \frac{\sin^2 \frac{x}{2}}{\left(\frac{x}{2}\right)^2} \cdot \frac{\left(\frac{x}{2}\right)^2}{x^2} \right) = 2 \cdot 1^2 \cdot \frac{1}{4} = \frac{1}{2}, \\
& \lim_{x \to 0} \frac{1}{\cos x} = 1.
\end{align*}

\textbf{Шаг 4.} Перемножаем результаты:
\[
1 \cdot \frac{1}{2} \cdot 1 = \frac{1}{2}.
\]

\hd{Ответ}
\ans{\dfrac{1}{2}}
```

## Оценка (заполняет Ярослав)

- Решено правильно? (да / нет / частично): 
- Нотация ВШЭ? (да / частично / нет): 
- Читаемо студенту? (да / нет): 
