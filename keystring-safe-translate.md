# План + СТАТУС: видимые строки-ключи переводить (консистентно), служебные защищать

Файл: `python-core/renpy_python_translator.py` (+ `selftest.py`). Цель: ключи сравнения по умолчанию
force-skip (защита кликов, УЖЕ работает), НО видимые игроку прозу-ключи (`murder weapon`,
`Death of the Author`, `The Artisan`, `type of killer`, `Roleplay`) — переводить и заменять ВО ВСЕХ файлах
одним переводом (чтобы `==` сходилось). Служебные коды (`home`/`voice`/`default`/шрифты/`he/him`) НЕ трогать.
Юзкейс: переводят ДО игры → старых сейвов нет → риск сейвов неактуален.

## УЖЕ СДЕЛАНО (в renpy_python_translator.py)
1. ✅ `find_comparison_keys(sources)` — собирает set строк в `== != in[...] dict-key .get()`. (~line 418)
2. ✅ Хук force-skip в main(): `comparison_keys = find_comparison_keys(sources)`, в цикле классификации
   `if entry["value"] in comparison_keys: skipped_keys += 1; continue` ПЕРЕД hard_skip. Лог + summary
   строка "Skipped as comparison keys (protected)". (main ~line 1503 + ~1521)
3. ✅ `_TRANSLATE_PARAMS` / `_TRANSLATE_LISTS` вынесены на модульный уровень; `hard_translate` использует их.
4. ✅ Предикаты добавлены после `find_comparison_keys` (~line 449):
   `_MIN_VISIBLE_LEN=3`, `_is_display_candidate`, `_looks_like_code_token`, `_visible_translatable_key`,
   `_likely_save_stored`.
5. ✅ selftest: `check_renpy_keystring_safety` существует и проходит (детект ключей). Зарегистрирован в main().

## ОСТАЛОСЬ СДЕЛАТЬ

### Шаг A — `apply_global_replacement` (после `apply_replacement`, ~line 1430 теперь)
Сигнатура:
```python
def apply_global_replacement(sources, value, translated_value, *,
                             game_path, extracted_from_archive, dry_run):
    """Replace EVERY exact quoted-literal occurrence of `value` across ALL source
    files with the wrapped translation (both quote styles), so a `==` on it still
    matches. Returns list[Path] of files modified. Backs up each, deletes .rpyc,
    mutates sources[...] in memory. Skips code-tokens defensively."""
```
Логика:
- `if _looks_like_code_token(value): return []`
- построить `dq = '"' + value.replace('\\','\\\\').replace('"','\\"') + '"'` и
  `sq = "'" + value.replace('\\','\\\\').replace("'","\\'") + "'"`; обёртки через `wrap_translation(translated_value, dq)` и `wrap_translation(translated_value, sq)`.
- по `for fpath, content in list(sources.items())`: для каждой пары (literal, wrapped) если `content.count(literal)`>0:
  - dry_run → залогировать, продолжить;
  - бэкап: `if fpath in extracted_from_archive: _backup_created(game_path, fpath) else: backup_file(game_path, fpath)` (один раз на файл);
  - `content = content.replace(literal, wrapped)`; пометить файл изменённым;
  - после обеих пар: записать файл (`open(fpath,"w",encoding="utf-8",newline="")`), `sources[fpath]=content`,
    удалить парный `.rpyc` (как в apply_replacement ~line 1367-1374).
- вернуть список изменённых Path.

### Шаг B — проводка в main()
- После вычисления `comparison_keys` (~line 1503): построить индекс
  `display_by_value = {}`; for e in all_candidates: display_by_value.setdefault(e["value"],[]).append(e).
  Вычислить tl/-покрытие: `from parsers.renpy import RenPyParser`; `import re`;
  `_norm = lambda s: re.sub(r"\s+"," ",s).strip()`;
  `displayed_via_tl = { _norm(s.original) for s in RenPyParser().extract(str(game_path)) }`.
  `visible_translatable_keys = { v for v in comparison_keys if _visible_translatable_key(v, display_by_value.get(v,[])) and _norm(v) not in displayed_via_tl }`.
  Лог: "Promoted N comparison keys to translation (visible prose)".
- В force-skip блоке: `if entry["value"] in comparison_keys and entry["value"] not in visible_translatable_keys: skip`.
  (т.е. промоутнутые НЕ скипаются — падают дальше в hard_translate/gemini как обычные.)
- ВАЖНО: чтобы промоут реально перевёлся и применился глобально — пометить его кандидатов. Проще:
  после классификации, перед apply-циклом, для каждого `v in visible_translatable_keys` гарантировать, что
  ОДИН представитель в `to_translate` с `entry["_global"]=True`. Дедуп по value.
- В apply-цикле (~ сейчас 1642-1671, сместилось): 
  `if entry.get("_global"): mods = apply_global_replacement(sources, original_val, translated_val, game_path=game_path, extracted_from_archive=set(extracted_from_archive), dry_run=args.dry_run); modified_extracted += [m for m in mods if m in set(extracted_from_archive)]; success_count+=1; continue`
  Обычные кандидаты — прежний путь.
- Лог save-риска: for v in visible_translatable_keys: if _likely_save_stored(v, sources): logger.warning("Translated save-stored key %r — old saves may not match; intended for fresh playthrough.", v).
- summary: добавить "Promoted visible keys: N".

ОСТОРОЖНО: промоутнутый value должен ПОПАСТЬ в translations (батч перевода). Если представитель в
to_translate с правильным value — `_translate_batch_raw` его переведёт (translations[value]). Проверить, что
apply-ветка берёт translated из того же translations dict.

### Шаг C — тест `check_renpy_keystring_promotion` (selftest.py, рядом с check_renpy_keystring_safety ~line 2266, зарегистрировать в main() после check_renpy_keystring_safety())
Кейсы:
1. `_visible_translatable_key("murder weapon", [display-candidate append→messages])` == True.
2. `_looks_like_code_token("home")`/`"voice"`/`"V"` == True; `"murder weapon"`/`"Roleplay"`/`"Death of the Author"` == False.
3. `apply_global_replacement(sources, "V", ...)` → [] (ничего).
4. Реальные temp .rpy: "The Artisan" сравнивается в logic.rpy, append в messages в chat.rpy; после
   apply_global_replacement ОБА файла содержат перевод, НИ один — англ. литерал; dummy .rpyc удалены;
   metadata.json имеет записи на оба файла.
5. tl/-покрытый value исключён из visible_translatable_keys (через мок displayed_via_tl или extract).

## ПРОВЕРКА
`npm run test:py` (зелёный) + `npm run check` (TS не трогаем, но прогнать). Финал — реальный прогон на
Killer Chat: `murder weapon`/`Death of the Author` по-русски; file.exe и режимы целы; игра не падает.
В логе sidecar: "Promoted ... : N".

## ВАЖНЫЕ ФАЙЛ-ОРИЕНТИРЫ (могли сместиться на ~30 строк после правок)
- `wrap_translation` (~line 1334 до правок) — обёртка перевода в кавычки нужного стиля.
- `apply_replacement` + его count>1 skip — НЕ трогать, промоуты идут мимо.
- `_backup_created` / `backup_file` — бэкап created/patch.
- main() apply-цикл со `modified_extracted`, Step 5b (удаление непереведённых extracted), Step 6 (компиляция).
- Запустить разово для проверки: `./venv/Scripts/python.exe -c "import renpy_python_translator as R; from pathlib import Path; s=R.load_all_sources(Path(r'G:\\SteamLibrary\\steamapps\\common\\Killer Chat! - Original Edition')); k=R.find_comparison_keys(s); print('murder weapon' in k, R._looks_like_code_token('home'), R._looks_like_code_token('murder weapon'))"`

## ПАМЯТЬ
Обновить/связать: [[renpy-inline-keystring-protection]] (добавить про промоут видимых),
[[translation-unknown-gender-neutral]], [[renpy-fixed-width-fit-hybrid]].
