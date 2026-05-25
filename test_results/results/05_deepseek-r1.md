# 05 improper_integral — deepseek-r1 (`openrouter/deepseek/deepseek-r1-0528`)

- **Уровень / тема:** средний / matan
- **thinking:** False
- **input tokens:** 1537
- **output tokens:** 818
- **cost:** 0.233 ₽
- **time:** 30.16 s
- **RAG top_sim:** 0.000 (hits=0)

## Условие

Вычислите несобственный интеграл $\displaystyle\int_{0}^{+\infty} x\,e^{-x^2}\,dx$.

## Эталонный ответ

$\dfrac{1}{2}$

## Ответ модели (raw)

```
\hd{Условие}  
Вычислить несобственный интеграл:  
$$ \int_{0}^{+\infty} x e^{-x^2}  dx. $$

\hd{Что найти}  
Значение интеграла \( \int_{0}^{+\infty} x e^{-x^2}  dx \).

\hd{Метод}  
Применяем замену переменной \( u = x^2 \) для сведения к табличному интегралу \(\int e^{-u}  du\).

\hd{Решение}  

\textbf{Шаг 1.} Введём замену переменной:  
$$ 
u = x^2, \quad du = 2x  dx, \quad \text{откуда} \quad x  dx = \frac{1}{2}  du.
$$

\textbf{Шаг 2.} Пересчитаем пределы интегрирования:  
\begin{align*}
x & = 0   \implies u = (0)^2 = 0, \\
x & \to +\infty  \implies u \to +\infty.
\end{align*}  
Исходный интеграл преобразуется:  
$$ 
\int_{0}^{+\infty} x e^{-x^2}  dx = \int_{0}^{+\infty} e^{-u} \cdot \frac{1}{2}  du = \frac{1}{2} \int_{0}^{+\infty} e^{-u}  du.
$$

\textbf{Шаг 3.} Вычисляем полученный несобственный интеграл:  
\begin{align*}
\int_{0}^{+\infty} e^{-u}  du &= \lim_{b \to +\infty} \int_{0}^{b} e^{-u}  du \\
&= \lim_{b \to +\infty} \left( -e^{-u} \Big|_{0}^{b} \right) \\
&= \lim_{b \to +\infty} \left( -e^{-b} - (-e^{0}) \right) \\
&= \lim_{b \to +\infty} \left( -e^{-b} + 1 \right).
\end{align*}  
Так как \( \lim_{b \to +\infty} e^{-b} = 0 \), имеем:  
$$ 
\lim_{b \to +\infty} \left( -e^{-b} + 1 \right) = 0 + 1 = 1.
$$

\textbf{Шаг 4.} Умножаем результат на \( \frac{1}{2} \):  
$$ 
\frac{1}{2} \int_{0}^{+\infty} e^{-u}  du = \frac{1}{2} \cdot 1 = \frac{1}{2}.
$$

\hd{Ответ}  
\ans{\dfrac{1}{2}}
```

## Оценка (заполняет Ярослав)

- Решено правильно? (да / нет / частично): 
- Нотация ВШЭ? (да / частично / нет): 
- Читаемо студенту? (да / нет): 
