# 10 series_convergence — sonnet (`claude-sonnet-4-6`)

- **Уровень / тема:** сложный / matan
- **thinking:** True
- **input tokens:** 2460
- **output tokens:** 2171
- **cost:** 10.297 ₽
- **time:** 28.69 s
- **RAG top_sim:** 0.847 (hits=5)

## Условие

Исследуйте на сходимость ряд $\displaystyle\sum_{n=1}^{\infty}\sin\bigl(\pi\sqrt{n^2+1}\bigr)$.

## Эталонный ответ

Сходится условно. $\sqrt{n^2+1} = n + \frac{1}{2n} + O(n^{-3})$, поэтому $\sin(\pi\sqrt{n^2+1}) = (-1)^n \sin\!\left(\frac{\pi}{2n} + O(n^{-3})\right) \sim (-1)^n \frac{\pi}{2n}$ — знакочередующийся ряд, сходится по Лейбницу; абсолютно расходится (как $\sum 1/n$).

## Ответ модели (raw)

```
\hd{Условие}

Исследовать на сходимость ряд
$$\sum_{n=1}^{\infty} \sin\bigl(\pi\sqrt{n^2+1}\bigr).$$

\hd{Что найти}

Сходится или расходится ряд; если сходится — указать характер (абсолютная/условная).

\hd{Метод}

Преобразуем аргумент синуса, выделив целую часть $\pi n$, и сведём задачу к знакочередующемуся ряду. Применим признак Лейбница и оценку остатка.

\hd{Решение}

\textbf{Шаг 1. Разложение $\sqrt{n^2+1}$ при больших $n$.}

Используем формулу $\sqrt{n^2+1} = n\sqrt{1+\tfrac{1}{n^2}}$ и разложение $\sqrt{1+t} = 1+\tfrac{t}{2}-\tfrac{t^2}{8}+\ldots$ при $t\to 0$:
$$\sqrt{n^2+1} = n + \frac{1}{2n} - \frac{1}{8n^3} + O\!\left(\frac{1}{n^5}\right).$$

Следовательно,
$$\pi\sqrt{n^2+1} = \pi n + \frac{\pi}{2n} - \frac{\pi}{8n^3} + O\!\left(\frac{1}{n^5}\right).$$

\textbf{Шаг 2. Упрощение $\sin\!\bigl(\pi\sqrt{n^2+1}\bigr)$.}

Применяем формулу синуса суммы $\sin(\pi n + \varphi) = \sin(\pi n)\cos\varphi + \cos(\pi n)\sin\varphi$, где
$$\varphi_n = \frac{\pi}{2n} - \frac{\pi}{8n^3} + O\!\left(\frac{1}{n^5}\right).$$

Поскольку $\sin(\pi n) = 0$ и $\cos(\pi n) = (-1)^n$, получаем:
$$\sin\!\bigl(\pi\sqrt{n^2+1}\bigr) = (-1)^n \sin\varphi_n.$$

\textbf{Шаг 3. Асимптотика $\sin\varphi_n$.}

Раскладываем синус при малом аргументе $\varphi_n \to 0$:
\begin{align*}
\sin\varphi_n &= \varphi_n - \frac{\varphi_n^3}{6} + \ldots \\
&= \frac{\pi}{2n} - \frac{\pi}{8n^3} - \frac{1}{6}\!\left(\frac{\pi}{2n}\right)^{\!3} + O\!\left(\frac{1}{n^5}\right) \\
&= \frac{\pi}{2n} + O\!\left(\frac{1}{n^3}\right).
\end{align*}

Таким образом, общий член ряда представляется в виде:
$$\sin\!\bigl(\pi\sqrt{n^2+1}\bigr) = (-1)^n\cdot\frac{\pi}{2n} + (-1)^n\cdot O\!\left(\frac{1}{n^3}\right).$$

\textbf{Шаг 4. Исследование сходимости.}

Запишем ряд как сумму двух рядов:
$$\sum_{n=1}^{\infty} \sin\!\bigl(\pi\sqrt{n^2+1}\bigr) = \underbrace{\sum_{n=1}^{\infty} \frac{(-1)^n \pi}{2n}}_{S_1} + \underbrace{\sum_{n=1}^{\infty} (-1)^n \cdot O\!\left(\frac{1}{n^3}\right)}_{S_2}.$$

\textit{Ряд $S_1$:} последовательность $b_n = \dfrac{\pi}{2n}$ монотонно убывает к нулю, поэтому по \textbf{признаку Лейбница} ряд $S_1$ \textbf{сходится}.

\textit{Ряд $S_2$:} существует константа $C>0$ такая, что $|(-1)^n \cdot O(n^{-3})| \leq \dfrac{C}{n^3}$, а ряд $\displaystyle\sum_{n=1}^{\infty}\dfrac{1}{n^3}$ сходится (гармонический ряд степени $p=3>1$), значит $S_2$ \textbf{сходится абсолютно}.

\textbf{Шаг 5. Вывод о характере сходимости.}

Поскольку $S_1$ сходится условно (ряд $\sum \dfrac{\pi}{2n}$ расходится), а $S_2$ сходится абсолютно, исходный ряд \textbf{сходится условно}.

\hd{Ответ}
\ans{\text{Ряд сходится условно (по признаку Лейбница для главной части } (-1)^n\tfrac{\pi}{2n}\text{)}.}
```

## Оценка (заполняет Ярослав)

- Решено правильно? (да / нет / частично): 
- Нотация ВШЭ? (да / частично / нет): 
- Читаемо студенту? (да / нет): 
