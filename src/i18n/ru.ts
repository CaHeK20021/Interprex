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
  onlyFreeModels: "Только бесплатные",
  threads: "Потоки",
  threadsHint:
    "Параллельных запросов на один ключ API. При 2 ключах столько на каждый. " +
    "Больше — быстрее, но следите за лимитом запросов в минуту у провайдера.",
  rpmLimit: "Лимит, зап/мин",
  rpmNoLimit: "нет",
  rpmLimitHint:
    "Лимит запросов в минуту НА КЛЮЧ для вашей модели (из панели провайдера). " +
    "Приложение само подстраивается, чтобы не превысить его — делит лимит между " +
    "потоками на каждом ключе, так что секунды вручную задавать не нужно. " +
    "Пусто — без лимита. (У всех облачных API ошибка 429/503 тоже тратит квоту, " +
    "поэтому повторы соблюдают тот же темп.)",
  workerLabel: (n: number) => `Поток ${n}`,
  workersToggleExpand: "Показать все потоки",
  workersToggleCollapse: "Свернуть потоки",
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
  statusTranslatingBatch: (num: number, size: number, elapsed: number, retry?: number) =>
    `Перевод пакета ${num} (${size} строк) — прошло ${elapsed} сек.` +
    (retry && retry > 1 ? ` (попытка ${retry}/100)` : "") +
    (elapsed > 15 ? " (ожидание ответа модели)" : ""),
  statusPaused: (num: number, size: number) =>
    `Перевод на паузе (пакет ${num}, ${size} строк)`,
  statusWaitingRetry: (num: number, size: number, retry: number, waitLeft: number) =>
    `Перевод пакета ${num} (${size} строк) — Ожидание перед повтором ${waitLeft} сек.` +
    (retry && retry > 0 ? ` (попытка ${retry}/100)` : ""),
  statusCompletedBatch: (num: number) => `Пакет ${num} успешно переведен!`,
  statusWaitingDelay: (waitLeft: number) => `Задержка — осталось ${waitLeft} сек.`,
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
  wroteBack: (n: number) => `Записано строк обратно в игру: ${n}.`,
  translateAborted: (done: number, total: number) =>
    `Перевод остановлен: модель перестала отвечать даже после повторов. ` +
    `Переведено ${done} из ${total} строк — почините движок и нажмите «Перевести» снова, чтобы доперевести остальное.`,
  translateErrors: (n: number) =>
    `Готово, но ${n} батч(ей) не перевелись и остались пустыми. Нажмите «Перевести» ещё раз, чтобы повторить их.`,
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
  noModsDetected: "Моды в этой папке не обнаружены.",
  selectAll: "Выбрать все",
  deselectAll: "Снять все",
  errNoModsSelected: "Пожалуйста, выберите хотя бы один мод.",
  errMixedEngines: "Выбраны моды с разными движками. Пожалуйста, выберите только моды одного типа.",
  phase_detecting_mods: "определяю моды",
  hintOpenModsFolder: "Откройте папку модов, чтобы извлечь строки из модов.",
  wroteBackMods: (n: number) => `Записано строк обратно в моды: ${n}.`,
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
