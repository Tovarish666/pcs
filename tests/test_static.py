#!/usr/bin/env python3
"""Скрипты панели (pcs/web/static/*.js) хотя бы разбираются: лишняя скобка в app.js — пустая
страница у всех (PR #27, 24.10.2026). Есть node — node --check; нет — свой разбор скобок
(строки, шаблоны `${}`, комментарии и регулярные выражения пропускаются).

    python3 tests/test_static.py
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, os.pardir, "pcs", "web", "static")
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


PAIRS = {")": "(", "]": "[", "}": "{"}
REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^") | {""}
REGEX_WORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "yield", "await"}


def brackets(src):
    """→ None или «строка N: что не так»."""
    stack, i, n, line = [], 0, len(src), 1
    tmpl = []                   # глубина стека, на которой открыт ${ … } шаблона
    prev = ""                   # последний значимый символ (или слово) — для «/ это регулярка?»

    def err(msg):
        return "строка %d: %s" % (line, msg)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                return err("комментарий /* не закрыт")
            line += src.count("\n", i, j)
            i = j + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    return err("строка не закрыта")
                j += 1
            i, prev = j + 1, "a"
            continue
        if c == "`" or (c == "}" and tmpl and tmpl[-1] == len(stack)):
            if c == "}":
                tmpl.pop()
            j = i + 1
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 1
                elif src.startswith("${", j):
                    break
                elif src[j] == "\n":
                    line += 1
                j += 1
            if j >= n:
                return err("шаблон ` не закрыт")
            if src[j] == "`":
                i, prev = j + 1, "a"
            else:
                tmpl.append(len(stack))
                i, prev = j + 2, "{"
            continue
        if c == "/" and (prev in REGEX_AFTER or prev in REGEX_WORDS):
            j, cls = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    return err("регулярное выражение не закрыто")
                if ch == "[":
                    cls = True
                elif ch == "]":
                    cls = False
                elif ch == "/" and not cls:
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            i, prev = j, "a"
            continue
        if c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if not stack:
                return err("лишняя «%s»" % c)
            o, at = stack.pop()
            if o != PAIRS[c]:
                return err("«%s» закрывает «%s» со строки %d" % (c, o, at))
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            prev = src[i:j]
            i = j
            continue
        prev = c
        i += 1
    if stack:
        return "не закрыта «%s» со строки %d" % stack[-1]
    return None


print("разбор скобок (на себе):")
check("целый код", brackets('const a = (x) => { return [x, `t${x + (1)}`, "a)", /\\(/.test(x)]; };') is None)
check("лишняя «)» — номер строки", brackets("f(a);\nel(b, c)));\n") == "строка 2: лишняя «)»", brackets("f(a);\nel(b, c)));\n"))
check("деление — не регулярка", brackets("const x = (a / b) / (c);") is None)
check("незакрытая — со строки", brackets("if (a) {\n  b();\n") == "не закрыта «{» со строки 1")

print("скрипты панели:")
node = shutil.which("node")
for path in sorted(glob.glob(os.path.join(STATIC, "*.js"))):
    name = os.path.basename(path)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    check("%s: скобки сходятся" % name, brackets(src) is None, brackets(src))
    if node:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        check("%s: node --check" % name, r.returncode == 0, (r.stderr or "")[-400:])

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
