# 04 limit — sonnet (`claude-sonnet-4-6`)

- **Уровень / тема:** средний / matan
- **thinking:** False
- **input tokens:** 2312
- **output tokens:** 796
- **cost:** 4.867 ₽
- **time:** 10.93 s
- **RAG top_sim:** 0.780 (hits=5)

## Условие

Вычислите предел $\displaystyle\lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3}$.

## Эталонный ответ

$\dfrac{1}{2}$

## Ответ модели (raw)

```
\hd{Условие}

Вычислить предел $\displaystyle\lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3}$.

\hd{Что найти}

Значение предела $\displaystyle\lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3}$.

\hd{Метод}

Разложим числитель, используя стандартные эквивалентности бесконечно малых при $x\to 0$:
$$\sin x \sim x - \frac{x^3}{6} + O(x^5), \qquad \operatorname{tg} x \sim x + \frac{x^3}{3} + O(x^5).$$

\hd{Решение}

\textbf{Шаг 1.} Преобразуем числитель алгебраически.

$$\operatorname{tg} x - \sin x = \frac{\sin x}{\cos x} - \sin x = \sin x \cdot \left(\frac{1}{\cos x} - 1\right) = \frac{\sin x \cdot (1 - \cos x)}{\cos x}.$$

\textbf{Шаг 2.} Подставляем в исходную дробь:

$$\frac{\operatorname{tg} x - \sin x}{x^3} = \frac{\sin x \cdot (1 - \cos x)}{x^3 \cos x}.$$

\textbf{Шаг 3.} Применяем стандартные эквивалентности при $x \to 0$:

$$\sin x \sim x, \qquad 1 - \cos x \sim \frac{x^2}{2}, \qquad \cos x \to 1.$$

\textbf{Шаг 4.} Вычисляем предел:

$$\lim_{x\to 0}\frac{\sin x \cdot (1 - \cos x)}{x^3 \cos x} = \lim_{x\to 0} \frac{\sin x}{x} \cdot \frac{1 - \cos x}{x^2} \cdot \frac{1}{\cos x}.$$

Каждый множитель имеет предел:
$$\lim_{x\to 0}\frac{\sin x}{x} = 1, \qquad \lim_{x\to 0}\frac{1-\cos x}{x^2} = \frac{1}{2}, \qquad \lim_{x\to 0}\frac{1}{\cos x} = 1.$$

Следовательно:
$$\lim_{x\to 0}\frac{\operatorname{tg} x - \sin x}{x^3} = 1 \cdot \frac{1}{2} \cdot 1 = \frac{1}{2}.$$

\hd{Ответ}
\ans{\dfrac{1}{2}}
```

## Оценка (заполняет Ярослав)

- Решено правильно? (да / нет / частично): 
- Нотация ВШЭ? (да / частично / нет): 
- Читаемо студенту? (да / нет): 
