# HANDOFF — доперевод модов Satisfactory (ContentLib). Читай целиком, ничего не ищи.

> Это передача состояния между сессиями. Тут ВСЁ: что нашли, почему оно ломается,
> что решили делать, точные файлы/строки, проверочные команды. Новый Claude должен
> понять с ходу и сразу начать кодить, без повторного расследования.

---

## 0. TL;DR (одним абзацем)

Перевод модов Satisfactory через ContentLib частично не доезжает до игры. **Корень —
коллизия stable id**: один `.uasset` содержит ДЕСЯТКИ строк с одинаковым
`(InternalPath, PropName)` (виджеты с кучей `Text`-полей; субсистема MkPlus с
массивом из 55 `Desc`). Наш `path=[InternalPath, PropName]` → один id на все →
**557 строк из 3859 схлопываются** (отсюда и расхождение 3328/3767 в прогресс-баре,
и непереведённые описания, и мусорные CDO-патчи с техническим значением). Решение из
двух частей: **(A) добавить разделитель в `path`** — `ExportName` для отдельных
экспортов и `ArrayIndex` для элементов массива (id станут уникальными, extract
перестанет терять строки); **(B) для struct-массивов субсистемы (MkPlus) генерировать
CDO с ПОЛНОЙ заменой массива** — только там, где все `Desc` имеют `HistoryType=Base`
(не трогать `StringTableEntry` — это локализация базовой игры). Пользователь выбрал
вариант **«Полная замена Base-массивов»** (см. §6).

---

## 1. Контекст проекта (минимум, остальное в CLAUDE.md)

- Interprex: Tauri + React/TS + Python-sidecar. Переводит строки в игровых движках.
- Движок `unreal4_5` = моды Satisfactory через **ContentLib** (JSON-патчи).
  Файл парсера: **`python-core/parsers/unreal4_5.py`**.
- Stable id = `make_id(engine, file, path, original)` (FNV-1a). `path[]` — адрес
  строки внутри файла, ОБЯЗАН быть уникальным, иначе строки делят id и теряются.
- ContentLib пишет JSON в `FactoryGame/Configs/ContentLib/{ItemPatches,RecipePatches,CDOs}/`
  (и дублируется в `Configs/ContentLib/...` для совместимости версий SML).
- Игра пользователя: **`G:\SteamLibrary\steamapps\common\Satisfactory`**.
- Лог-оракул (единственная правда о применении патчей):
  **`%LOCALAPPDATA%/FactoryGame/Saved/Logs/FactoryGame.log`** — печатает
  `[CL] Processed /Configs/ ItemPatches Successful: N/M` и причину каждого реджекта.
  Веб-поиск по формату ContentLib ВРЁТ — доверяй только логу + клон-доке.
- Клон доки ContentLib: `/tmp/cl_doc/` (если пропал —
  `git clone https://github.com/budak7273/ContentLib_Documentation /tmp/cl_doc`).
  Ключевые файлы: `modules/ROOT/pages/Features/CDOs.adoc`, `JsonSchemas/CL_CDO.json`.
- Исходник ContentLib (как именно применяются патчи):
  `https://raw.githubusercontent.com/Nogg-aholic/ContentLib/master/Source/ContentLib/Private/CLCDOBPFLib.cpp`

---

## 2. Инструменты и пути (всё уже на месте)

- **retoc**: `python-core/bin/retoc.exe`. Распаковывает IoStore (`.utoc`/`.ucas`).
  - `to-legacy <indir> <outdir> [--version VER_UE5_3] [--no-shaders --no-script-objects]`
    — собирает legacy `.uasset`+`.uexp`. Флаги `--no-shaders --no-script-objects`
    дают −50% времени, 0 потерь ассетов (уже добавлены в коде).
  - `info <PATH>` — ПОЗИЦИОННЫЙ путь (НЕ `--path`!). `list --path`, `get`.
  - Для сборки мода нужны `global.utoc`+`global.ucas` из
    `<game>/FactoryGame/Content/Paks/` (симлинкуй в temp/in, не копируй — огромные).
- **UAssetExtractor** (C#, UAssetAPI 1.1.0, net10.0):
  - Исходник: `python-core/uasset-extractor/Program.cs`.
  - Бинарь (используется sidecar'ом): `python-core/bin/UAssetExtractor.exe`.
  - Сборка: `cd python-core/uasset-extractor && dotnet build -c Release`, потом
    скопировать `bin/Release/net10.0/UAssetExtractor.{exe,dll,pdb}` в `python-core/bin/`.
  - Режимы: `--input <file>` (один ассет) и `--input-dir <dir>` (батч, один процесс
    на всю папку — основной путь скорости). `--output <json>` или stdout.
    `--engine VER_UE5_3` (моды пользователя — UE5.3).
  - **Я уже добавил в `ExtractedItem` поля** (нужны для фикса §5):
    `HistoryType` (None/Base/StringTableEntry), `Namespace`, `ArrayIndex` (-1 если
    не из массива), `ExportName` (имя экспорта, например `ApplyBtnText`).
    Эти поля УЖЕ эмитятся (extractor пересобран и задеплоен в `bin/`).
  - **Я уже добавил** обработку `TArray<FText>` и `TArray<FStr>` элементов в
    `ExtractProps` (раньше массивы рекурсились ТОЛЬКО в struct-элементы). Но на
    MkPlus это не сработало (там Desc вложен в struct внутри struct-массива —
    `ArrayIndex` оказался -1), см. §4.
  - **`--dump-json` я добавлял для диагностики и УЖЕ УДАЛИЛ** — в проде его нет.

---

## 3. ГЛАВНАЯ ПРОБЛЕМА — коллизия id (измерено, это №1 по важности)

`path=[InternalPath, PropName]` НЕ уникален. Замер по реальному прогону 36 модов
(`.opt_work/opt3_stream.ndjson`):

```
всего строк (rows):                 3859
уникальных id-ключей (file+path):   3210
ключей с >1 разным значением:        121   ← КОЛЛИЗИИ
строк ПОТЕРЯНО на коллизии:          557   ← делят id с другими, перевод теряется
```

**Почему так** — два структурных случая:

### Случай A: виджет с кучей отдельных Text-экспортов (массовый)
Пример `SmartFoundations/Smart_SettingsForm_Widget`: 83 строки, но всего
**2 уникальных `(InternalPath, PropName)`** и **64 уникальных значения**. Каждое
текстовое поле — ОТДЕЛЬНЫЙ экспорт (`ApplyBtnText`, `ApplyImmediatelyLabel`, …),
но `InternalPath` у всех один (путь ассета), `PropName` почти у всех = `Text`.
→ 64 значения схлопываются в 2 id. **Разделитель есть: `ExportName` (83 уникальных).**

Другие пострадавшие (prop повторяется >3x в одном ассете): PowerChecker
(`Text` x21, x13, x10…), SmartFoundations (`Text` x82, `Tooltip` x36, `DisplayName`
x31), SpaceMarket2, Loaders, ContentLib(`Widget_CL_Config` Text x9), AB_FluidExtras
(`DisplayName` x23), InfiniteNudge, и т.д. — полный список был получен скриптом в §8.

### Случай B: субсистема MkPlus — struct-массив с 55 Desc (особый)
`MkPlus/BP_MkPlusSubsystem`, свойство `Desc` встречается **55 раз** — это НЕ
top-level массив, а поле `Desc` внутри struct'ов внутри 7 struct-массивов CDO
(подробно §4). Все 55 → один id. **Разделитель: `ArrayIndex` + имя массива.**

---

## 4. ПОДРОБНО про MkPlus (BP_MkPlusSubsystem) — структура и ограничения

Дерево свойств (получено через UAssetAPI SerializeJson, путь в дереве
`Exports[1].Data[*]`):

```
Default__BP_MkPlusSubsystem_C (это CDO, ExportName = Default__BP_MkPlusSubsystem_C)
 ├─ factory      : Array<struct MkPlus_Factory>[21]
 ├─ variablePower: Array<struct>[6]
 ├─ generator    : Array<struct>[8]
 ├─ vehicle      : Array<struct>[3]
 ├─ trainStation : Array<struct>[6]
 ├─ droneStation : Array<struct>[3]
 └─ storage      : Array<struct>[5]
 каждый struct = {
    Buildable  : Object  -> /MkPlus/.../Build_*  (_C)
    Cvar       : Str     ("ConstructorMk2")        ← технический
    Desc       : FText                              ← ОПИСАНИЕ (то, что переводим)
    Vol        : Int
    Recipe     : Object  -> /MkPlus/.../Recipe_* (_C)
    Ingredients: Array<struct {ItemClass:Object, Amount:Int}>
 }
```

Типы `Desc` (FText `HistoryType`) ПО МАССИВАМ — это решает, что можно трогать:

| массив         | структур | Desc-типы                         | можно перевести? |
|----------------|---------:|-----------------------------------|------------------|
| droneStation   |        3 | всё **Base**                      | ✅ ДА            |
| generator      |        8 | всё **Base**                      | ✅ ДА            |
| trainStation   |        6 | всё **Base**                      | ✅ ДА            |
| storage        |        5 | всё **Base**                      | ✅ ДА            |
| vehicle        |        3 | всё **None** (пустые)             | — нечего         |
| factory        |       21 | 16 StringTableEntry + 5 Base      | ⚠️ пропустить    |
| variablePower  |        6 | всё StringTableEntry              | ⚠️ пропустить    |

- **Base** = литеральное английское описание, написанное модом → переводим.
- **StringTableEntry** = ссылка на строковую таблицу БАЗОВОЙ игры (значение вида
  `Production/Constructor/Description`). Движок САМ показывает это на языке игрока.
  Трогать НЕЛЬЗЯ — иначе захардкодим английский и сломаем локализацию базы.
- Описания построек MkPlus живут ТОЛЬКО здесь. На `Build_*`/`Desc_*`/`Recipe_*`
  ассетах есть только `mDisplayName` (имя), описаний там нет. Поэтому
  «Functions as home Port…» (дрон-порт) и «Transports up to 1200…» (поезд) —
  это `droneStation`/`trainStation`, оба Base → переводимы.

### Почему текущий код пишет МУСОР
Сейчас inject делает CDO-патч `{"Property":"Desc","Value":<один перевод>}`. Но:
1. у класса `BP_MkPlusSubsystem_C` НЕТ top-level свойства `Desc` (оно вложено) →
   патч бесполезен;
2. дедуп по имени свойства (`_seen`) берёт ПЕРВОЕ из 55 значений = технический
   `Production/Constructor/Description` → в патч уходит мусор.
В логе при этом `Processed CDOs Successful: 1305/1305` (применилось, но дрянью).

### Ограничение ContentLib CDO (проверено по доке И исходнику CLCDOBPFLib.cpp)
- CDO умеет заменить **только ВЕСЬ массив целиком**. Индекс/поле элемента —
  НЕЛЬЗЯ. Цитата доки CDOs.adoc:331 «does not allow modifying specific keys or
  appending new keys — you must replace the entire array».
- При замене массива каждый элемент создаётся с НУЛЯ (default), и `EditCDO`
  ставит ТОЛЬКО те ключи struct'а, что есть в JSON; отсутствующие остаются
  дефолтными. ⇒ если в struct указать только `Desc`, то `Buildable/Recipe/Cvar/
  Vol/Ingredients` ОБНУЛЯТСЯ → субсистема сломается. **Надо эмитить ПОЛНЫЙ struct.**
- FText в массиве парсится через `FText::FromString(json string)` — простая
  строка. StringTable-ссылки при замене теряются (ещё причина не трогать
  StringTableEntry-массивы).
- Object-поля (`Buildable`,`Recipe`,`Ingredients.ItemClass`): `EditCDO` ждёт
  СТРОКУ-путь, грузит `LoadObject`. Формат: полный путь класса, напр.
  `/MkPlus/Buildables/DroneStation/Build_DroneStation_Mk2.Build_DroneStation_Mk2_C`.
  - ⚠️ **ПРОБЛЕМА**: `Ingredients[].ItemClass` в собранном legacy-ассете
    резолвится в `/Engine/UnknownPackage.UnknownExport` (ссылки на предметы
    БАЗОВОЙ игры, retoc их не именует — и С флагами, и БЕЗ них, проверено). Значит
    при полной замере массива `Ingredients` мы НЕ сможем восстановить ItemClass.
    **Но `Ingredients` к описаниям отношения не имеет.** Варианты для новой сессии:
    (а) пропускать массивы, где `Ingredients.ItemClass` нерезолвим (безопасно, но
    теряем эти описания); (б) проверить — может, если в JSON НЕ указывать
    `Ingredients` вовсе, движок оставит дефолтные ингредиенты из Blueprint'а
    (тогда полная замена массива безопасна и для описаний). Нужно ПРОВЕРИТЬ НА
    ЖИВОЙ ИГРЕ по логу — это единственная правда. Скорее всего (б) не сработает,
    т.к. при замене массива struct создаётся с нуля, Ingredients станет пустым →
    рецепт постройки сломается. ⇒ безопаснее: НЕ трогать subsystem-массивы вообще,
    если не докажешь на игре, что Ingredients можно не указывать. См. §6 риски.

---

## 5. ЧТО ДЕЛАТЬ (план реализации)

### Часть A — ОБЯЗАТЕЛЬНА, чинит 557 потерянных строк (главное)
Сделать `path[]` уникальным, добавив разделитель из новых полей экстрактора.

**Extract** (два места строят path, ОБА поправить одинаково):
- `parsers/unreal4_5.py:145` (`_process_utoc_worker`, основной utoc-путь):
  сейчас `path_key = [item["InternalPath"], item["PropName"]]`.
- `parsers/unreal4_5.py:1035` (`_extract_from_uassets`, pak-ветка): сейчас
  `path=[item["InternalPath"], item["PropName"]]`.

Новый path (предложение, согласуй формат и держи ОДИНАКОВЫМ в обоих местах):
```python
seg = item.get("ExportName") or ""
ai  = item.get("ArrayIndex", -1)
if ai is not None and ai >= 0:
    path_key = [item["InternalPath"], item["PropName"], f"{seg}#{ai}"]
elif seg:
    path_key = [item["InternalPath"], item["PropName"], seg]
else:
    path_key = [item["InternalPath"], item["PropName"]]
```
Цель: чтобы для SmartFoundations-виджета 64 значения дали 64 разных id, а не 2.
ПРОВЕРКА после: повторить замер коллизий (§8) — «строк потеряно» должно стать ~0
(останутся лишь честные дубли: одинаковое значение в одном месте).

⚠️ **`path` входит в id** → это СМЕНА id для уже переведённых строк. Это ожидаемо
и нормально (старые id были «склеенными» и битыми). Translation memory просто
переведёт по-новой. Если пользователь жалуется на потерю прогресса — это цена
исправления коллизии, предупреди его.

**Inject** (`_inject_into_uassets`, читает `s.path[0]`, `s.path[1]` на строках
1173-1174): теперь path может иметь 3 элемента. `prop_name = s.path[1]` остаётся,
третий элемент — разделитель, в большинстве веток (ItemPatch/RecipePatch по
`Desc_*`/`Recipe_*` ассетам, у них обычно ОДИН экспорт) не нужен. НО:
- Для CDO-веток, где один ассет = много экспортов с одним PropName (виджеты), CDO
  патчит свойство НА КЛАССЕ — а тут это разные под-объекты (sub-widgets), не
  свойства корневого класса. **Скорее всего виджет-Text вообще НЕ патчится через
  CDO корневого класса.** Это надо осознать: возможно, многие из этих 557 строк
  технически непатчабельны в игре (виджеты), но их хотя бы перестанет ТЕРЯТЬ на
  извлечении/в таблице, и не будет битых патчей. Проверь на логе, что битых
  CDO-патчей не появляется.

### Часть B — субсистема MkPlus (вариант, ВЫБРАННЫЙ пользователем: «Полная замена Base-массивов»)
Сгенерировать CDO с полной заменой ТОЛЬКО тех struct-массивов, где ВСЕ `Desc` =
`HistoryType=Base` (droneStation/generator/trainStation/storage). Для каждого
такого массива:
1. собрать ПОЛНЫЕ struct'ы всех его элементов (Buildable, Cvar, Desc(перевод),
   Vol, Recipe, Ingredients) — значения брать из дерева ассета;
2. `Desc` подменить на перевод, остальные поля — как в оригинале;
3. написать `{"Property": "<имяМассива>", "Value": [ <struct>, ... ]}`.

⚠️ **РИСК Ingredients** (см. §4): `ItemClass` базовых предметов нерезолвим
(`UnknownExport`). Если воссоздать struct без рабочего `Ingredients`, рецепт
постройки сломается. **ДО реализации части B проверь на живой игре**: собери ОДИН
CDO для `droneStation` с полными struct'ами, поставь в игру, глянь
`FactoryGame.log` (применилось ли, не сломались ли рецепты дрон-порта). Если
Ingredients ломается — нужен план: либо тянуть полные пути ItemClass из БАЗОВОЙ
игры (тяжело), либо признать subsystem-описания непереводимыми и сделать только
часть A + §6-«убрать мусор». **Не пиши часть B вслепую — она рискованная.**

Для извлечения нужны новые поля экстрактора, чтобы отличить Base от StringTableEntry
и узнать имя массива/индекс. `ExportName` для subsystem = `Default__..._C` (один на
все 55), поэтому для РАЗЛИЧЕНИЯ внутри субсистемы нужен составной разделитель —
имя массива + индекс. **ВНИМАНИЕ**: текущий extractor НЕ кладёт имя родительского
массива в item (для top-level массива клал бы `PropName`=имя массива, но тут Desc
вложен в struct, и `ArrayIndex` вышел -1!). Нужно ДОРАБОТАТЬ `Program.cs`:
протаскивать имя ближайшего массива и индекс элемента в этом массиве при рекурсии
в struct (сейчас `ExtractProps(insideStruct=true)` теряет позицию). Это отдельная
доработка C# до части B.

### Часть C — минимум-фоллбэк, если часть B окажется слишком рискованной
Просто перестать писать битый CDO-патч субсистемы и исключить мусорные значения:
- не генерировать CDO для `BP_MkPlusSubsystem` (или для любого ассета, где один
  PropName повторяется и значения — StringTable-ссылки);
- отфильтровать значения-ссылки (`Production/.../Description` и т.п.) — это путь
  со слешами без пробелов. ⚠️ ТЕКУЩИЙ `LooksLikeIdentifier` в `Program.cs` ловит
  ТОЛЬКО точки (2+) и hex-суффиксы, а слеши НЕ ловит → `Production/Constructor/
  Description` проходит фильтр. Можно добавить отсев «слеш-путь без пробелов, не
  начинается на `/`, нет букв вне сегментов», но ОСТОРОЖНО — не зарезать реальные
  строки с «/». Лучше отсев по `HistoryType==StringTableEntry` (надёжно: это
  явный признак ссылки), который теперь доступен в item.

---

## 6. Решение пользователя

На вопрос «как чинить субсистему» пользователь выбрал **«Полная замена Base-массивов»**
(часть B). НО он не видел риска Ingredients (выяснился после). Перед реализацией B
обязательно: (1) сделать часть A (она бесспорна и чинит большинство), (2) проверить
B на одном массиве на живой игре по логу, (3) если Ingredients ломается — вернуться
к пользователю с этим фактом и предложить часть C как фоллбэк.

Изначальная жалоба пользователя: непереведены «Transports up to 1200 resources per
minute» (trainStation) и «Functions as home Port to a single Drone…» (droneStation).
Оба — Base, оба в субсистеме, оба чинятся частью B (если Ingredients не помешает).

---

## 7. Текущее состояние кода (что УЖЕ изменено, незакоммичено)

`git status` (значимое):
- **`python-core/uasset-extractor/Program.cs`** — ИЗМЕНЁН:
  - в `ExtractedItem` добавлены поля `HistoryType`, `Namespace`, `ArrayIndex`,
    `ExportName`;
  - `ExtractProps` теперь обрабатывает `TArray<FText>` и `TArray<FStr>` элементы
    (не только struct); пишет HistoryType/Namespace/ArrayIndex;
  - в `ProcessAsset` после обработки каждого экспорта проставляется `ExportName`
    всем добавленным item'ам;
  - `--dump-json` (диагностика) ДОБАВЛЯЛСЯ и УЖЕ УДАЛЁН.
- **`python-core/bin/UAssetExtractor.{exe,dll,pdb}`** — ПЕРЕСОБРАНЫ и задеплоены
  (соответствуют новому Program.cs).
- `python-core/parsers/unity.py` — изменён НЕ мной в этой задаче (был в diff
  изначально, не трогать в рамках этой задачи).
- `src-tauri/Cargo.{toml,lock}` — не наше, не трогать.
- `parsers/unreal4_5.py` — ⚠️ **ЕЩЁ НЕ ИЗМЕНЁН под новый path** (часть A не сделана).
  Сейчас extract на :145 и :1035 всё ещё строит `path=[InternalPath, PropName]`.
  Новые поля экстрактора приходят в item, но НЕ используются. **Это первый шаг.**

Эталон/замеры в `.opt_work/` (gitignored): `mods.json` (36 путей модов),
`baseline_ids.json` (старый golden), `opt3_stream.ndjson` (последний прогон extract).

---

## 8. Проверочные команды (копируй-вставляй)

Запуск sidecar (после правок Python ОБЯЗАТЕЛЬНО рестарт — `--reload` не всегда
ловит): `taskkill /F /IM sidecar.exe; taskkill /F /IM python.exe` затем
`python-core/venv/Scripts/python.exe python-core/main.py`.

Пересборка экстрактора:
```
cd python-core/uasset-extractor && dotnet build -c Release
cp bin/Release/net10.0/UAssetExtractor.exe ../bin/UAssetExtractor.exe
cp bin/Release/net10.0/UAssetExtractor.dll ../bin/UAssetExtractor.dll
cp bin/Release/net10.0/UAssetExtractor.pdb ../bin/UAssetExtractor.pdb
```

Замер коллизий id (главная метрика части A — должно стать ~0 потерь):
```python
import json
from collections import defaultdict
seen=defaultdict(set); total=0
with open(r'C:\Users\Alexandr\Desktop\Interprex\.opt_work\opt3_stream.ndjson',encoding='utf-8') as f:
    for line in f:
        line=line.strip()
        if not line: continue
        ev=json.loads(line)
        if ev.get('type')!='mod': continue
        for s in ev.get('strings',[]):
            total+=1
            seen[(s.get('file',''),tuple(s.get('path',[])))].add(s.get('original',''))
print('rows',total,'idkeys',len(seen),
      'collisions',sum(1 for v in seen.values() if len(v)>1),
      'lost',sum(len(v)-1 for v in seen.values() if len(v)>1))
# СЕЙЧАС: rows 3859 idkeys 3210 collisions 121 lost 557. ЦЕЛЬ после части A: lost ~0.
```
(После правок нужен СВЕЖИЙ прогон extract в новый ndjson — старый opt3 построен
старым path'ом. Прогон: через UI «сканировать моды», или /extract_stream по
`.opt_work/mods.json`, root = `G:\SteamLibrary\steamapps\common\Satisfactory`.)

Сборка одного мода вручную (для дампа структуры; пример MkPlus):
```bash
TMP=$(mktemp -d); mkdir -p "$TMP/in"
PAK="G:/SteamLibrary/steamapps/common/Satisfactory/FactoryGame/Mods/GameFeatures/MkPlus/Content/Paks/Windows"
GLOB="G:/SteamLibrary/steamapps/common/Satisfactory/FactoryGame/Content/Paks"
cp "$PAK"/*.utoc "$PAK"/*.ucas "$TMP/in/"
ln -s "$GLOB/global.utoc" "$TMP/in/global.utoc"; ln -s "$GLOB/global.ucas" "$TMP/in/global.ucas"
python-core/bin/retoc.exe to-legacy "$TMP/in" "$TMP/out" --version VER_UE5_3 --no-shaders --no-script-objects
# ассет: $TMP/out/FactoryGame/Mods/GameFeatures/MkPlus/Content/BP_MkPlusSubsystem.uasset
```
⚠️ Python для Windows НЕ понимает `/tmp/...` и `/c/...` пути — копируй файлы в
`C:\Users\Alexandr\Desktop\Interprex\python-core\` и открывай по `C:\...` пути.

Извлечь с диагностикой:
```
python-core/bin/UAssetExtractor.exe --input <asset> --engine VER_UE5_3 --output out.json
# в out.json теперь есть HistoryType / Namespace / ArrayIndex / ExportName
```

Самотесты движка: `cd python-core && venv/Scripts/python.exe selftest.py`
(проверки `check_unreal4_5*`). После правок path — добавь регресс-кейс на коллизию.

---

## 9. Порядок действий для новой сессии (чек-лист)

1. Прочитать этот файл целиком + раздел `unreal4_5` в CLAUDE.md.
2. **Часть A** (бесспорно): поправить `path` на :145 и :1035 в `unreal4_5.py`
   (добавить ExportName/ArrayIndex-разделитель, формат одинаковый в обоих местах).
   Поправить чтение в inject (path может быть длиной 3).
3. Рестарт sidecar, свежий прогон extract, замер коллизий (§8) → `lost` ≈ 0.
4. Проверить, что битые CDO-патчи не появляются (лог игры), таблица показывает
   все строки (3328→ближе к реальному уникальному числу без склейки).
5. **Часть B** (рискованно, выбор пользователя): доработать `Program.cs` —
   протаскивать имя массива + индекс при рекурсии в struct (для различения 55
   Desc субсистемы). Собрать CDO с полной заменой ОДНОГО Base-массива
   (droneStation), поставить в игру, проверить лог (Ingredients не сломались?).
   - сломалось → к пользователю с фактом, предложить часть C (фоллбэк).
   - ок → раскатать на все Base-массивы (droneStation/generator/trainStation/
     storage), StringTableEntry/None-массивы НЕ трогать.
6. Самотесты + регресс-кейс на коллизию id.
7. Не коммитить без явной просьбы пользователя.

---

## 10. Чего НЕ делать (грабли)

- НЕ использовать `retoc --filter` (фильтр по подстроке имени — теряет
  prefixless-ассеты, нарушает инвариант «0 потерь строк»).
- НЕ трогать `StringTableEntry`-описания (сломаешь локализацию базовой игры).
- НЕ писать частичный struct в CDO-замену массива (обнулит Buildable/Recipe/
  Ingredients → сломает постройку).
- НЕ доверять веб-поиску по формату ContentLib — только лог игры + клон-дока.
- НЕ забыть рестарт sidecar после правок Python и пересборку+деплой exe после
  правок Program.cs.
- Python-for-Windows не ест `/tmp` и `/c/` пути — работай через `C:\...`.
- `subsys.json` в `python-core/` — СТАРЫЙ закоммиченный файл, не трогать/не путать
  с диагностическими дампами (те я удалил).
