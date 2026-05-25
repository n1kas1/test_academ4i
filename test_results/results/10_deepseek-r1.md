# 10 series_convergence — deepseek-r1 (`openrouter/deepseek/deepseek-r1-0528`)

- **Уровень / тема:** сложный / matan
- **thinking:** False
- **input tokens:** 2001
- **output tokens:** 3375
- **cost:** 0.765 ₽
- **time:** 113.97 s
- **RAG top_sim:** 0.847 (hits=5)

## Условие

Исследуйте на сходимость ряд $\displaystyle\sum_{n=1}^{\infty}\sin\bigl(\pi\sqrt{n^2+1}\bigr)$.

## Эталонный ответ

Сходится условно. $\sqrt{n^2+1} = n + \frac{1}{2n} + O(n^{-3})$, поэтому $\sin(\pi\sqrt{n^2+1}) = (-1)^n \sin\!\left(\frac{\pi}{2n} + O(n^{-3})\right) \sim (-1)^n \frac{\pi}{2n}$ — знакочередующийся ряд, сходится по Лейбницу; абсолютно расходится (как $\sum 1/n$).

## Ответ модели (raw)

```
\hd{Условие}
Исследовать на сходимость ряд \(\sum_{n=1}^{\infty} \sin\left(\pi \sqrt{n^2 + 1}\right)\).

\hd{Что найти}
Сходимость ряда (абсолютную или условную) или расходимость.

\hd{Метод}
Используем асимптотическое разложение общего члена ряда и признаки сходимости для знакочередующихся рядов и рядов с положительными членами.

\hd{Решение}

\textbf{Шаг 1.} Преобразуем выражение под знаком синуса. Для \(\sqrt{n^2 + 1}\) при \(n \to \infty\):
\[
\sqrt{n^2 + 1} = n \sqrt{1 + \frac{1}{n^2}} = n \left(1 + \frac{1}{2n^2} - \frac{1}{8n^4} + O\left(\frac{1}{n^6}\right)\right) = n + \frac{1}{2n} - \frac{1}{8n^3} + O\left(\frac{1}{n^5}\right).
\]

\textbf{Шаг 2.} Подставим в аргумент синуса:
\[
\pi \sqrt{n^2 + 1} = \pi \left(n + \frac{1}{2n} - \frac{1}{8n^3} + O\left(\frac{1}{n^5}\right)\right) = \pi n + \frac{\pi}{2n} - \frac{\pi}{8n^3} + O\left(\frac{1}{n^5}\right).
\]

\textbf{Шаг 3.} Упростим синус, используя тождество \(\sin(\pi n + \theta) = (-1)^n \sin \theta\):
\[
\sin\left(\pi \sqrt{n^2 + 1}\right) = (-1)^n \sin\left(\frac{\pi}{2n} - \frac{\pi}{8n^3} + O\left(\frac{1}{n^5}\right)\right).
\]

\textbf{Шаг 4.} Разложим синус в ряд Тейлора (\(\sin \theta = \theta - \frac{\theta^3}{6} + O(\theta^5)\) при \(\theta \to 0\)):
\[
\sin\left(\frac{\pi}{2n} - \frac{\pi}{8n^3} + O\left(\frac{1}{n^5}\right)\right) = \left(\frac{\pi}{2n} - \frac{\pi}{8n^3}\right) - \frac{1}{6} \left(\frac{\pi}{2n}\right)^3 + O\left(\frac{1}{n^5}\right).
\]
Вычислим куб:
\[
\left(\frac{\pi}{2n}\right)^3 = \frac{\pi^3}{8n^3}, \quad \frac{1}{6} \cdot \frac{\pi^3}{8n^3} = \frac{\pi^3}{48n^3}.
\]
Таким образом:
\[
\sin\left(\cdots\right) = \frac{\pi}{2n} - \frac{\pi}{8n^3} - \frac{\pi^3}{48n^3} + O\left(\frac{1}{n^5}\right) = \frac{\pi}{2n} - \frac{\pi(6 + \pi^2)}{48n^3} + O\left(\frac{1}{n^5}\right).
\]

\textbf{Шаг 5.} Подставим в общий член ряда:
\[
\sin\left(\pi \sqrt{n^2 + 1}\right) = (-1)^n \left(\frac{\pi}{2n} - \frac{\pi(6 + \pi^2)}{48n^3} + O\left(\frac{1}{n^5}\right)\right) = (-1)^n \frac{\pi}{2n} + (-1)^n \cdot C \cdot \frac{1}{n^3} + O\left(\frac{1}{n^5}\right),
\]
где \(C = -\frac{\pi(6 + \pi^2)}{48}\) — константа.

\textbf{Шаг 6.} Представим исходный ряд как сумму трёх рядов:
\[
\sum_{n=1}^{\infty} \sin\left(\pi \sqrt{n^2 + 1}\right) = \underbrace{\sum_{n=1}^{\infty} (-1)^n \frac{\pi}{2n}}_{\text{(I)}} + \underbrace{\sum_{n=1}^{\infty} (-1)^n \cdot C \cdot \frac{1}{n^3}}_{\text{(II)}} + \underbrace{\sum_{n=1}^{\infty} O\left(\frac{1}{n^5}\right)}_{\text{(III)}}.
\]

\textbf{Шаг 7.} Анализ сходимости:
\begin{itemize}
\item Ряд (I): \(\sum_{n=1}^{\infty} (-1)^n \frac{\pi}{2n}\) — знакочередующийся гармонический ряд. Сходится условно по признаку Лейбница, так как \(\frac{1}{n}\) монотонно убывает к 0.
\item Ряд (II): \(\sum_{n=1}^{\infty} (-1)^n \cdot C \cdot \frac{1}{n^3}\) сходится абсолютно, так как \(\sum |a_n| = |C| \sum \frac{1}{n^3}\) — обобщённый гармонический ряд с показателем \(3 > 1\).
\item Ряд (III): \(\sum_{n=1}^{\infty} O\left(\frac{1}{n^5}\right)\) сходится абсолютно, так как \(\sum \frac{1}{n^5}\) сходится (\(5 > 1\)).
\end{itemize}
Сумма сходящегося условно и двух абсолютно сходящихся рядов сходится условно.

\textbf{Шаг 8.} Проверим абсолютную сходимость исходного ряда. Рассмотрим ряд из модулей:
\[
\sum_{n=1}^{\infty} \left| \sin\left(\pi \sqrt{n^2 + 1}\right) \right|.
\]
Из асимптотики:
\[
\left| \sin\left(\pi \sqrt{n^2 + 1}\right) \right| \sim \frac{\pi}{2n} \quad \text{при} \quad n \to \infty.
\]
По предельной форме признака сравнения:
\[
\lim_{n \to \infty} \frac{\left| \sin\left(\pi \sqrt{n^2 + 1}\right) \right|}{\frac{1}{n}} = \lim_{n \to \infty} n \cdot \left| (-1)^n \frac{\pi}{2n} + O\left(\frac{1}{n^3}\right) \right| = \frac{\pi}{2} > 0.
\]
Так как гармонический ряд \(\sum \frac{1}{n}\) расходится, то ряд из модулей также расходится.

\hd{Ответ}
\ans{\text{Ряд сходится условно}}
```

## Оценка (заполняет Ярослав)

- Решено правильно? (да / нет / частично): 
- Нотация ВШЭ? (да / частично / нет): 
- Читаемо студенту? (да / нет): 
