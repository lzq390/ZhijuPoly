#!/usr/bin/env python3
"""Rebuild deterministic, synthetic handoff assets. Never reads scientific data."""
from pathlib import Path
import csv
import hashlib
import io
import json
from xml.sax.saxutils import escape
import zipfile

BASE = Path(__file__).resolve().parent
PUBLIC = BASE / 'public'
DOWNLOADS = PUBLIC / 'handoff/downloads'
DIANHYDRIDE = 'O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O'


def csv_bytes(rows):
    output = io.StringIO(newline='')
    csv.writer(output).writerows(rows)
    return output.getvalue().encode('utf-8-sig')


def archive(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, data)


def xlsx(path, rows):
    cells = []
    for row_index, row in enumerate(rows, 1):
        row_cells = ''.join(f'<c r="{chr(65 + col)}{row_index}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>' for col, value in enumerate(row))
        cells.append(f'<row r="{row_index}">{row_cells}</row>')
    archive(path, {
        '[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="synthetic" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(cells) + '</sheetData></worksheet>',
    })


def main():
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    for role, smile in [('a', 'Nc1ccc(N)cc1'), ('b', DIANHYDRIDE)]:
        rows = [['id', 'name', 'SMILES'], ['001', f'Synthetic monomer {role.upper()}', smile]]
        (DOWNLOADS / f'{role}.csv').write_bytes(csv_bytes(rows))
        xlsx(DOWNLOADS / f'{role}.xlsx', rows)
    results = [['pair_id', 'monomer_a_id', 'monomer_b_id', 'status', 'polymer_smiles', 'synthetic'], ['DEMO-001', '001', '001', 'success', '*CC*', 'true']]
    result_csv = csv_bytes(results)
    (DOWNLOADS / 'result.csv').write_bytes(result_csv)
    (DOWNLOADS / 'input_errors.csv').write_bytes(csv_bytes([['role', 'row', 'message']]))
    xlsx(DOWNLOADS / 'results.xlsx', results)
    archive(DOWNLOADS / 'results.zip', {'results.csv': result_csv, 'README.txt': 'Synthetic frontend demonstration only. No calculation or measured data.\n'})
    dft_result = (BASE / 'source/dft-result.json').read_bytes()
    (DOWNLOADS / 'scientific_result.json').write_bytes(dft_result)
    archive(DOWNLOADS / 'dft-bundle.zip', {'scientific_result.json': dft_result, 'README.txt': 'Synthetic DFT frontend demonstration only. No calculation or measured data.\n'})
    (DOWNLOADS / 'result.json').write_text(json.dumps({'synthetic': True, 'units': 'angstrom', 'frames': [{'symbols': ['C', 'C'], 'positions': [[0, 0, 0], [1.5, 0, 0]]}]}, indent=2) + '\n')
    (DOWNLOADS / 'result.xyz').write_text('2\nSynthetic frontend demo\nC 0.000 0.000 0.000\nC 1.500 0.000 0.000\n')
    (DOWNLOADS / 'result.sdf').write_text('Synthetic handoff demo\n  NexPoly\n\n  2  1  0  0  0  0            999 V2000\n    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n    1.5000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n  1  2  1  0  0  0  0\nM  END\n$$$$\n')
    (DOWNLOADS / 'result.cube').write_text('Synthetic scalar field\nFrontend demonstration only\n  1 0.0 0.0 0.0\n  2 1.0 0.0 0.0\n  2 0.0 1.0 0.0\n  2 0.0 0.0 1.0\n  6 6.0 0.0 0.0 0.0\n 0.1 0.2 0.1 0.2 0.1 0.2 0.1 0.2\n')
    (DOWNLOADS / 'result.txt').write_text('Synthetic frontend demonstration only. No scientific result.\n')

    rows = []
    for target_index, target in enumerate(['tg', 'cte', 'elongation', 'modulus']):
        for i in range(12):
            chain = 'C' * (i + 2)
            rows.append({'targetKey': target, 'piId': 900001 + target_index * 12 + i, 'monomerASmiles': DIANHYDRIDE, 'monomerBSmiles': 'N' + chain + 'N', 'polymerSmiles': '*N1C(=O)c2cc3c(cc2C1=O)C(=O)N(' + chain + '*)C3=O', 'tgCelsius': 205 + (i * 3 + target_index * 4) % 43, 'linearExpansion': (34 + (i * 3 + target_index) % 25) / 1_000_000, 'hardness': 5 + (i + target_index) % 12, 'homoLumoGapEv': round(2.2 + i / 10, 2), 'refractiveIndex': round(1.5 + i / 100, 2)})
    types = '''// HANDOFF: generated synthetic examples; not database records or scientific predictions.
export type HighThroughputDoePiTargetKey = "tg" | "cte" | "elongation" | "modulus";
export type HighThroughputDoePiSourceRow = {
  targetKey: HighThroughputDoePiTargetKey;
  piId: number;
  monomerASmiles: string;
  monomerBSmiles: string;
  polymerSmiles: string;
  tgCelsius: number;
  linearExpansion: number | null;
  hardness: number | null;
  homoLumoGapEv: number | null;
  refractiveIndex: number | null;
};

'''
    source = BASE / 'source/highThroughputDoePiRows.ts'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(types + 'export const PI_DOE_SOURCE_ROWS = ' + json.dumps(rows, indent=2) + ' as const satisfies readonly HighThroughputDoePiSourceRow[];\n')

    sample_dir = PUBLIC / 'samples/monomer-polymerization-batch'
    sample_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for family in ['diamines', 'dianhydrides']:
        sample_rows = [['id', 'name', 'SMILES', 'source_row', 'source_label']]
        for i in range(120):
            smiles = 'N' + 'C' * (2 + i % 12) + 'N' if family == 'diamines' else DIANHYDRIDE
            sample_rows.append([f'DEMO-{family[:2]}-{i + 1:03}', f'Synthetic {family} {i + 1:03}', smiles, i + 1, 'synthetic'])
        data = csv_bytes(sample_rows)
        filename = f'smipoly-{family}-120.csv'
        (sample_dir / filename).write_bytes(data)
        files.append({'name': filename, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    (sample_dir / 'selection.json').write_text(json.dumps({'synthetic': True, 'source_file': 'Generated synthetic handoff examples', 'source_records': 0, 'selection': 'Generated rows; repeated molecules deliberately support bulk-layout testing. Not database samples.', 'counts': {'diamines': {'selected': 120, 'unique_valid_structures': 12}, 'dianhydrides': {'selected': 120, 'unique_valid_structures': 1}}, 'files': files, 'expected_pairs': 14400, 'encoding': 'UTF-8 BOM', 'mapping': {'smiles_column': 'SMILES', 'id_column': 'id', 'name_column': 'name'}, 'target': 'polyimide'}, indent=2) + '\n')
    (sample_dir / 'README.md').write_text('# 合成批量导入样例\n\n沿用现有文件名以兼容界面，内容为本交付包生成的合成样例，不来自原始 SMiPoly 数据库。两表各 120 行；结构有意重复，仅用于导入、分页和状态样式演示，不用于科学计算。原始来源路径和数据指纹已移除。\n')
    papers = PUBLIC / 'images/pdf-similarity'
    papers.mkdir(parents=True, exist_ok=True)
    for i in range(1, 7):
        lines = ''.join(f'<rect x="64" y="{210 + y * 24}" width="{460 - (y % 4) * 40}" height="8" rx="4" fill="#dce5ed"/>' for y in range(18))
        (papers / f'similar_paper_{i}.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800" viewBox="0 0 600 800"><rect width="600" height="800" fill="#f8fafc"/><rect x="24" y="24" width="552" height="752" rx="12" fill="white" stroke="#dce5ed"/><text x="64" y="91" font-family="sans-serif" font-size="15" fill="#64748b">NEXPOLY / SYNTHETIC EXAMPLE</text><text x="64" y="140" font-family="sans-serif" font-size="28" fill="#0f172a">Demonstration paper {i}</text><text x="64" y="175" font-family="sans-serif" font-size="14" fill="#64748b">Layout placeholder — no source article content</text>{lines}<rect x="64" y="690" width="472" height="40" rx="5" fill="#ecfeff"/><text x="80" y="716" font-family="sans-serif" font-size="13" fill="#0f766e">Synthetic frontend handoff asset</text></svg>\n')
    print(f'Generated {len(rows)} synthetic DOE rows, batch samples, downloads and 6 SVG placeholders.')


if __name__ == '__main__':
    main()
