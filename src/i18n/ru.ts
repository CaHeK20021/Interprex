// Russian UI strings. Must provide exactly the same keys as en.ts — the
// `Strings` type in index.ts makes a missing or misspelled key a compile error.

import type { Strings } from "./index";

const ru: Strings = {
  // header
  appTagline: "Локализация игр от и до",
  sidecarOnline: "движок подключён",
  sidecarOffline: "движок не запущен",
  uiLanguage: "Интерфейс",
  targetLanguage: "Переводить на",
  lang_Russian: "Русский",
  lang_English: "Английский",
  lang_Spanish: "Испанский",
  lang_German: "Немецкий",
  lang_French: "Французский",
  lang_Japanese: "Японский",
  lang_Chinese_Simplified: "Китайский (упрощенный)",
  lang_Korean: "Корейский",
  lang_Portuguese_Brazil: "Португальский (Бразилия)",
  fontStyle: "Шрифт",
  fontStyleSmooth: "Обычный",
  fontStylePixel: "Пиксельный",
  fontStyleSmoothHint:
    "Обычный шрифт (Noto) — чистый и хорошо читаемый. Подходит большинству игр.",
  fontStylePixelHint:
    "Пиксельный шрифт (битмап) — под стиль пиксель-арт игр. Латиница/кириллица — " +
    "PixelOperator, китайский/японский — Zpix. Корейский и скрипты без пиксельного " +
    "шрифта используют обычный.",
  provider: "Движок",
  model: "Модель",
  apiKey: "API-ключ",
  apiKey2: "Второй API-ключ",
  optional: "необязательно",
  addKey: "добавить ключ",
  removeKey: "удалить ключ",
  // кастомный браузер папок
  fpTitle: "Открыть папку игры",
  fpDrives: "Диски",
  fpUp: "На уровень вверх",
  fpLoading: "Загрузка…",
  fpEmpty: "Здесь нет подпапок",
  fpNoDrives: "Диски не найдены",
  fpPathPlaceholder: "Вставьте или введите путь к папке…",
  fpGo: "Перейти",
  fpCancel: "Отмена",
  fpChoose: "Выбрать эту папку",
  fpChooseHint: "Сначала откройте папку, затем выберите её",
  fpRemember: "Запоминать путь",
  fpRememberGameTooltip: "Сохранять выбранный путь к игре для автоматического открытия",
  fpRememberModsTooltip: "Сохранять выбранный путь к модам для автоматического открытия",
  fpSidebarThisPC: "Этот компьютер",
  fpSidebarHome: "Папка пользователя",
  fpSidebarLibraries: "Библиотеки игр",
  fpSidebarDownloads: "Загрузки",
  fpSidebarDesktop: "Рабочий стол",
  fpSidebarDocuments: "Документы",
  fpSidebarLocalDisk: (drive: string) => `Локальный диск ${drive.replace(":", "")}`,
  baseUrl: "Адрес сервера",
  modelPlaceholderLocal: "напр. llama3.1",
  modelPlaceholderGemini: "gemini-2.5-flash",
  modelLoading: "ищу модели…",
  modelAutoActive: "активная (авто)",
  modelTypeManually: "Ввести имя модели…",
  modelNeedKey: "введите API-ключ, чтобы загрузить список моделей",
  modelCheckingKey: "проверяю API-ключ…",
  modelBadKey: "ключ отклонён — моделей для него нет",
  maxBatchSize: "Размер пакета",
  maxBatchSizeHint: "Максимальное количество строк, отправляемых в одном запросе к API",
  groupSmallFiles: "Группировать мелкие файлы",
  groupSmallFilesHint: "Смарт-группировка фрагментированных файлов. Авто: объединяет батчи, если есть 8+ файлов с менее чем 5 строками. Вкл: объединять всегда. Выкл: переводить пофайлово.",
  groupSmallFilesAuto: "Автоматически",
  groupSmallFilesOn: "Всегда включено",
  groupSmallFilesOff: "Выключено",
  onlyFreeModels: "Только бесплатные",
  threads: "Потоки",
  threadsHint:
    "Параллельных запросов на один ключ API (1–40). При 2 ключах столько на каждый " +
    "(всего потоков = потоки × ключи). Больше — быстрее, но следите за RPM и TPM, " +
    "чтобы pacing не вылезал за квоту.",
  rpmLimit: "Лимит, зап/мин",
  rpmNoLimit: "нет",
  rpmLimitHint:
    "Лимит запросов в минуту НА КЛЮЧ для вашей модели (из панели провайдера). " +
    "Приложение само подстраивается, чтобы не превысить его — делит лимит между " +
    "потоками на каждом ключе, так что секунды вручную задавать не нужно. " +
    "Пусто — без лимита. (У всех облачных API ошибка 429/503 тоже тратит квоту, " +
    "поэтому повторы соблюдают тот же темп.)",
  tpmLimit: "TPM, K/мин",
  tpmNoLimit: "нет",
  httpTimeout: "Ждать, сек",
  httpTimeoutHint:
    "Сколько секунд ждать ОДИН ответ API, потом обрываем сокет сами. " +
    "Это наш лимит, не их — NVIDIA free Gemma может висеть в очереди минутами. " +
    "По умолчанию 200. От 10 до 3600.",
  tpmLimitHint:
    "Лимит токенов в минуту НА КЛЮЧ в тысячах (K). Пиши 16 = 16 000 ток/мин " +
    "(бесплатный Gemini). Приложение оценивает пакет по тексту; потоки одного " +
    "ключа делят окно 60 с. Другие ключи независимы. Пусто — без TPM pacing " +
    "(RPM, если задан, всё равно действует).",
  tpmMeterTitle: "TPM (окно 60 с, на ключ)",
  tpmMeterHint:
    "Живой расход токенов за последние 60 с на каждом API-ключе. Used = spent " +
    "(завершённые запросы) + reserved (в полёте). Free = сколько ещё можно " +
    "отправить. Если free высокий, а потоки ждут — пиши, это баг.",
  tpmMeterDetail: (spent: string, reserved: string, free: string) =>
    `spent ${spent} + R ${reserved} · free ${free}`,
  workerLabel: (n: number) => `Поток ${n}`,
  workersToggleExpand: "Показать все потоки",
  workersToggleCollapse: "Свернуть потоки",
  sessionLogBtn: "Лог",
  sessionLogBtnHint:
    "Этот запуск: успешные батчи + ошибки, сгруппированные со счётчиком (×N). " +
    "Сбрасывается при новом «Перевести».",
  sessionLogTitle: "Сеанс перевода",
  sessionLogEmpty: "Пока нет запуска. Нажми «Перевести» — статистика появится здесь вживую.",
  sessionLogSuccess: (batches: number, strings: number) =>
    `Успешно: ${batches} батч(ей) × ${strings} строк`,
  sessionLogErrorsTitle: (attempts: number, kinds: number) =>
    attempts === 0
      ? "Ошибки"
      : `Неудачных попыток: ${attempts}${kinds > 1 ? ` · ${kinds} вида` : ""}`,
  sessionLogNoErrors: "Ошибок пока нет.",
  sessionLogMeta: (provider: string, model: string, root: string) =>
    `${provider}${model ? " · " + model : ""}${root ? "\n" + root : ""}`,
  sessionLogClose: "Закрыть",
  sessionLogRequests: (n: number) => `Запросов отправлено: ${n}`,
  openrouterDailyUsage: (used: number, cap: number) =>
    `Бесплатных запросов сегодня: ${used} / ${cap}`,
  openrouterDailyUsageHint:
    "Использовано запросов к бесплатным моделям за сегодня от дневного лимита " +
    "(50, или 1000 при пополнении на $10+). Считается локально — учитывается " +
    "каждый дошедший до сервера запрос, включая ошибочные. Сброс в полночь UTC.",

  // buttons
  openFolder: "Открыть папку игры…",
  translate: "Перевести",
  writeBack: "Записать в игру",
  restoreOriginal: "Восстановить оригинал",
  restoreOriginalHint: "Вернуть оригинальные файлы игры из бэкапа",
  deleteBackup: "Удалить бэкап",
  deleteBackupHint: "Подтвердить перевод и окончательно удалить бэкап",
  exportZip: "Экспортировать перевод (ZIP)",
  exportZipHint: "Запаковать все файлы перевода в ZIP-архив для отправки",
  confirmDiscardBackupTitle: "Удаление бэкапа",
  confirmDiscardBackup: "Вы уверены, что хотите удалить бэкап? Вы больше не сможете восстановить оригинальные файлы игры.",
  confirmDiscardBackupOk: "Да, удалить",
  confirmDiscardBackupCancel: "Отмена",
  btnPause: "Пауза",
  btnResume: "Продолжить",
  translatePythonBtn: "Перевести Python-строки",
  translatePythonBtnHint: "Перевести встроенные строки в коде Python ($ блоки, init python)",
  translatePythonTitle: "Перевод Python-строк Ren'Py",
  btnDryRun: "Симуляция (Dry Run)",
  btnRealRun: "Выполнить перевод",

  // phases
  phase_detecting: "определяю движок",
  phase_extracting: "извлекаю строки",
  phase_translating: "перевожу",
  phase_paused: "пауза",
  phase_pausing: "останавливаюсь (дописываю пакеты)",
  phase_saving: "сохраняю",
  phase_backing_up: "создаю бэкап",
  phase_injecting: "записываю в игру",
  phase_autofixing: "проверяю и чиню перевод",
  phase_restoring: "восстанавливаю",
  phase_deleting_backup: "удаляю бэкап",
  autofixFixed: (n: number) => `Автофикс исправил ${n} строк(и) после перевода.`,

  // overflow risk + engine-lint
  riskDialogueTitle: "Риск переполнения диалогов:",
  lintHazardTitle: (n: number) =>
    `Проверка движком самой игры нашла ${n} реальную(ых) проблему(ы) в переводе:`,

  // progress
  progressLabel: (done: number, total: number) =>
    `${done} / ${total} строк`,
  statusInitializing: "Инициализация переводчика...",
  statusTranslatingBatch: (_num: number, size: number, elapsed: number, retry?: number) =>
    `Перевод (${size} строк)` +
    (elapsed > 0 ? ` — ${elapsed} сек` : "") +
    (retry && retry > 1 ? ` (попытка ${retry}/100)` : "") +
    (elapsed > 15 ? " (ждём модель)" : ""),
  // Always free at the claim gate — owned batches finish before parking here.
  statusPaused: (_num: number, _size: number) => "Пауза — жду продолжения",
  statusFinishingBatch: (_num: number, size: number, elapsed: number) =>
    `Дописываю текущий запрос (${size} строк) — ${elapsed} сек`,
  statusWaitingRetry: (_num: number, _size: number, _retry: number, waitLeft: number) =>
    `Сбой. Повтор через ${waitLeft}с — строки целы`,
  statusNetworkRetry: (_tryN: number, _size: number) =>
    `Сеть отвалилась. Повтор — строки целы`,
  statusCompletedBatch: (_num: number) => `Пакет готов`,
  // After a successful batch: keep that fact + show RPM pacing countdown.
  statusWaitingDelay: (waitLeft: number, batchNum?: number) =>
    batchNum
      ? `Пакет ${batchNum} переведен — ожидание ${waitLeft} сек`
      : `Ожидание ${waitLeft} сек`,
  // waitLeft=0 → sibling still holds a reserve (no honest countdown).
  // waitLeft>0 → real 60s window drain. free/est optional context from ledger.
  statusWaitingTpm: (
    waitLeft: number,
    batchNum?: number,
    _free?: number,
    _est?: number,
  ) =>
    waitLeft > 0
      ? batchNum
        ? `Пакет ${batchNum} — ждём TPM ${waitLeft} сек`
        : `Ждём TPM ${waitLeft} сек`
      : batchNum
        ? `Пакет ${batchNum} — ждём слот TPM`
        : `Ждём слот TPM`,
  statusResting: "Отдыхает (ждёт работу)",
  statusWorkerError: "Ключ не сработал",
  pyStatusWaiting: "Ожидание...",
  pyStatusClassifying: "Оценка необходимости перевода...",
  pyStatusClassified: "Оценка завершена",
  pyStatusTranslating: "Перевод...",
  pyStatusFinished: "Завершено",
  pyStatusBatchDone: (phase: string, cur: string, total: string) => `${phase} пакет ${cur}/${total}`,
  pyStatusError: (phase: string) => `Ошибка: ${phase} не удалась`,
  pyStatusBatchError: (num: number) => `Ошибка перевода пакета ${num}`,
  // Двухстадийный прогресс Python-строк: стадия 1 оценивает, какие кандидаты надо
  // переводить (число точное, но это НЕ сколько переведём — это станет известно
  // только после оценки); стадия 2 переводит подтверждённые строки.
  pyProgressClassify: (done: number, total: number) =>
    `Оценка ${done} / ${total} кандидатов`,
  pyProgressTranslate: (done: number, total: number) =>
    `Перевод ${done} / ${total} строк`,
  pyClassified: "Классифицирован",
  pyTranslated: "Переведён",
  statusDone: "Готово",
  showingRows: (from: number, to: number, total: number) =>
    `${from}–${to} из ${total}`,
  pageOf: (page: number, pages: number) => `Страница ${page} из ${pages}`,

  // table
  colOriginal: "Оригинал",
  colTranslation: "Перевод",
  colWhere: "Где",

  // messages
  hintOpenFolder: "Откройте папку игры, чтобы извлечь строки.",
  hintReadyToTranslatePython: "Готово к переводу. Выберите действие внизу. Симуляция покажет, какие строки будут переведены, без изменения файлов игры.",
  errNoEngine: "В этой папке не найден поддерживаемый движок.",
  wroteBack: (n: number) => `Записано в игру уникальных строк: ${n} (повторяющиеся объединены — это не потеря перевода).`,
  translateAborted: (done: number, total: number) =>
    `Перевод остановлен: модель перестала отвечать даже после повторов. ` +
    `Переведено ${done} из ${total} строк — почините движок и нажмите «Перевести» снова, чтобы доперевести остальное.`,
  translateErrors: (n: number, done: number, total: number) =>
    `Перевод остановлен: ${n} батч(ей) упали (ошибка API / ключ / провайдер). ` +
    `В этом запуске сохранено ${done} из ${total} строк — остальное не переведено. ` +
    `Исправьте ключ/провайдер и нажмите «Перевести» снова. Запись в игру и перевод Python-строк НЕ запускались.`,
  translateSuccess: (unique: number, total: number) =>
    total > unique
      ? `Применено к ${total} ячейкам (уникальных: ${unique})`
      : `Переведено ячеек: ${unique}`,
  backupStatusLabel: "Создан бэкап:",
  restoreSuccess: "Оригинальные файлы успешно восстановлены!",
  deleteBackupSuccess: "Резервная копия удалена.",
  exportZipSuccess: (name: string) => `Перевод успешно запакован в архив:\n${name}\n(файл выбран в проводнике)`,
  exportZipFail: (err: string) => `Не удалось экспортировать архив: ${err}`,

  // mods mode
  modeGame: "Локализация игры",
  modeMods: "Локализация модов",
  openModsFolder: "Открыть папку модов…",
  detectedModsLabel: "Найденные моды",
  modNameHeader: "Имя мода",
  modStatsHeader: "Строки",
  modStringsHeader: "Строки",
  modProgressHeader: "Прогресс",
  modStatusHeader: "Статус",
  statusNotStarted: "Не начато",
  statusInProgress: "В процессе",
  statusCompleted: "Готово",
  statusAlreadyTranslated: "Уже переведено",
  statusNoStrings: "Нет строк",
  statusExtracting: "Извлечение",
  stringsCalculating: "подсчёт",
  noModsDetected: "Моды в этой папке не обнаружены.",
  allMods: "Все моды",
  selectAll: "Выбрать все",
  deselectAll: "Снять все",
  errNoModsSelected: "Пожалуйста, выберите хотя бы один мод.",
  errMixedEngines: "Выбраны моды с разными движками. Пожалуйста, выберите только моды одного типа.",
  phase_detecting_mods: "определяю моды",
  hintOpenModsFolder: "Откройте папку модов, чтобы извлечь строки из модов.",
  wroteBackMods: (n: number) => `Записано в моды уникальных строк: ${n} (повторяющиеся объединены — это не потеря перевода).`,
  writeBackBtnMods: "Записать в моды",

  // proxy settings panel
  proxySettingsTitle: "Прокси / Свой сервер",
  proxyUrlLabel: "Адрес прокси",
  proxyUrlPlaceholder: "https://username-space-name.hf.space/v1",
  proxyUrlHint: "Оставьте пустым для использования официального сервера",
  proxyInfoTitle: "Как настроить бесплатный прокси (для пользователей из РФ и других регионов с блокировками)",
  proxyInfoStep1Suffix: " — следуйте инструкциям для создания Space",
  proxyInfoStep2: "2. Войдите через Hugging Face (бесплатно) и сделайте Duplicate Space (это развернет собственный контейнер без лимитов по времени)",
  proxyInfoStep3: "3. Скопируйте прямой URL вашего Space (напр. https://username-space-name.hf.space/v1) и вставьте выше",
  proxyInfoStep4: "4. Выберите провайдер (например, Google Gemini) — при ошибках блокировки запросы пойдут через Hugging Face Space автоматически.",
  proxyInfoFree: "Полностью бесплатно · Ваш собственный контейнер на Hugging Face · Работает с Gemini, OpenAI, Groq и другими",
  proxySave: "Сохранить и проверить",
  proxyChecking: "Проверка…",
  proxyDone: "Готово",
  proxyCheckFailed: "Проверка не удалась — прокси недоступен. Проверьте URL.",
  proxyModeDirect: "напрямую (прокси не нужен)",
  proxyModeProxy: "через прокси",
  proxyModeUnknown: "недоступно ни так, ни так",

  // auto-update overlay
  updateChecking: "Проверка обновлений…",
  updateDownloading: "Скачивание обновления {version}…",
  updateReady: "Обновление готово. Перезапуск…",
  updateLatest: "У вас последняя версия",
  updateError: "Не удалось проверить обновления",
  updateProgress: "{downloaded} / {total} МБ",
};

export default ru;
