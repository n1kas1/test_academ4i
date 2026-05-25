# 01 derivative — deepseek-r1 (`openrouter/deepseek/deepseek-r1-0528`)

- **Уровень / тема:** простой / matan
- **thinking:** False
- **input tokens:** 1908
- **output tokens:** 768
- **cost:** 0.239 ₽
- **time:** 27.06 s
- **RAG top_sim:** 0.853 (hits=5)

## Условие

Найдите производную функции $y = \ln\bigl(\sin(x^2)\bigr)$.

## Эталонный ответ

$y' = \dfrac{2x\cos(x^2)}{\sin(x^2)} = 2x\,\operatorname{ctg}(x^2)$

## Ответ модели (raw)

```
\hd{Условие}  
Найти производную функции $y = \ln(\sin(x^2))$.

\hd{Что найти}  
Производную $\frac{dy}{dx}$.

\hd{Метод}  
Использовать правило дифференцирования сложной функции и цепное правило. Функция представлена композицией: $y = \ln(u)$, где $u = \sin(v)$, $v = x^2$.

\hd{Решение}  

\textbf{Шаг 1.} Введём промежуточные переменные:  
Пусть $v = x^2$, тогда $u = \sin(v)$, и $y = \ln(u)$.  

\textbf{Шаг 2.} Найдём производные промежуточных функций:  
$$  
\frac{dv}{dx} = 2x, \quad  
\frac{du}{dv} = \cos(v), \quad  
\frac{dy}{du} = \frac{1}{u}.  
$$  

\textbf{Шаг 3.} Применим цепное правило:  
$$  
\frac{dy}{dx} = \frac{dy}{du} \cdot \frac{du}{dv} \cdot \frac{dv}{dx} = \frac{1}{u} \cdot \cos(v) \cdot 2x.  
$$  

\textbf{Шаг 4.} Подставим выражения для $u$ и $v$:  
$$  
\frac{dy}{dx} = \frac{1}{\sin(v)} \cdot \cos(v) \cdot 2x = \frac{\cos(x^2)}{\sin(x^2)} \cdot 2x,  
$$  
так как $v = x^2$.  

\textbf{Шаг 5.} Упростим выражение:  
$$  
\frac{dy}{dx} = 2x \cdot \cot(x^2),  
$$  
где $\cot(\cdot)$ — котангенс.  

\hd{Ответ}  
\ans{  
$$  
\frac{dy}{dx} = 2x \cot(x^2)  
$$  
}
```

## Оценка (заполняет Ярослав)

- Решено правильно? (да / нет / частично): 
- Нотация ВШЭ? (да / частично / нет): 
- Читаемо студенту? (да / нет): 
