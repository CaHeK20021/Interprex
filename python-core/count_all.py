import os
import sys
import io

# Настраиваем UTF-8 для консоли Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))
from parsers import get_parser
from parsers.renpy import RenPyParser

QSP_PATH = r"C:\GOG Games\QSP games\SaraGame\SaraGame 1\SaraGame_1.4.4"
RENPY_PATH = r"C:\GOG Games\QSP games\SaraGame\SaraGame 2\SaraGame2-0.8-pc"

def count_qsp():
    print("=== QSP (SaraGame 1) ===")
    parser = get_parser("qsp")
    strings = parser.extract(QSP_PATH)
    
    # Группируем по файлам
    by_file = {}
    for s in strings:
        by_file[s.file] = by_file.get(s.file, 0) + 1
        
    for frel in sorted(by_file.keys()):
        fpath = os.path.join(QSP_PATH, frel)
        total_fields = 0
        try:
            with open(fpath, "rb") as f:
                text = f.read().decode("utf-16le")
            total_fields = len(text.split("\r\n"))
        except Exception:
            pass
        print(f"Файл: {frel:<15} | Полей в файле: {total_fields:<5} | Извлечено строк: {by_file[frel]}")
        
    print(f"ИТОГО QSP строк: {len(strings)}")

def count_renpy():
    print("\n=== Ren'Py (SaraGame 2) ===")
    parser = RenPyParser()
    
    # Получаем все .rpy файлы через внутренний поиск парсера
    rpy_files = parser._rpy_files(RENPY_PATH)
    strings = parser.extract(RENPY_PATH)
    
    # Группируем извлеченные строки по файлам
    extracted_by_file = {}
    for s in strings:
        extracted_by_file[s.file] = extracted_by_file.get(s.file, 0) + 1
        
    total_lines_sum = 0
    total_extracted_sum = 0
    
    for fpath in rpy_files:
        frel = os.path.relpath(fpath, RENPY_PATH).replace("\\", "/")
        
        # Считаем физические строки кода
        line_count = 0
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                line_count = len(f.readlines())
        except Exception as e:
            print(f"Ошибка чтения {frel}: {e}")
            
        extracted_count = extracted_by_file.get(frel, 0)
        print(f"Файл: {frel:<18} | Строк кода: {line_count:<5} | Извлечено строк: {extracted_count}")
        
        total_lines_sum += line_count
        total_extracted_sum += extracted_count
        
    print(f"ИТОГО Ren'Py: файлов: {len(rpy_files)} | строк кода: {total_lines_sum} | извлечено строк: {total_extracted_sum}")

if __name__ == "__main__":
    count_qsp()
    count_renpy()
