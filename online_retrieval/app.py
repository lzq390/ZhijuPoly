from flask import Flask, request, jsonify, send_from_directory, render_template_string
from flask_cors import CORS
import re
import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
import pandas as pd
import numpy as np
from datetime import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from io import BytesIO
import base64

app = Flask(__name__)
CORS(app)

# 确保静态文件夹存在
STATIC_FOLDER = os.path.join(os.path.dirname(__file__), 'static')
if not os.path.exists(STATIC_FOLDER):
    os.makedirs(STATIC_FOLDER)

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['Arial', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# 历史记录文件路径
HISTORY_FILE = os.getenv(
    'HISTORY_FILE',
    os.path.join(os.path.dirname(__file__), 'search_history.json')
)

def load_history():
    """加载搜索历史"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []

def save_history(history):
    """保存搜索历史"""
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except:
        pass

def save_search_result(material, mode, result_data):
    """保存完整的搜索结果"""
    history = load_history()
    
    # 检查是否已存在相同材料的记录，如果存在则更新
    existing_index = None
    for i, item in enumerate(history):
        if item.get('material') == material and item.get('mode') == mode:
            existing_index = i
            break
    
    history_entry = {
        'material': material,
        'mode': mode,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'papers_found': result_data.get('totalPapers', 0),
        'reactions_extracted': len(result_data.get('syntheses', [])),
        'max_papers': result_data.get('max_papers', 500),
        'result_data': {
            'stats': result_data.get('stats', {}),
            'syntheses': result_data.get('syntheses', []),
            'tempPlot': result_data.get('tempPlot'),
            'solventPie': result_data.get('solventPie'),
            'reactionPlot': result_data.get('reactionPlot'),
            'catalystTable': result_data.get('catalystTable', []),
            'tempLabels': result_data.get('tempLabels', []),
            'conditionSummary': result_data.get('conditionSummary', []),
            'reactionTypeTable': result_data.get('reactionTypeTable', []),
            'dataframe': result_data.get('dataframe', [])
        }
    }
    
    if existing_index is not None:
        history[existing_index] = history_entry
    else:
        history.insert(0, history_entry)
    
    # 只保留最近条记录
    history = history[:100]
    save_history(history)
    
    return history_entry


# ==================== 温度标准化函数 ====================
def standardize_temperature(temp_str):
    if temp_str is None or pd.isna(temp_str):
        return None, None
    
    temp_str = str(temp_str).lower().strip()
    
    temp_mappings = {
        'room temperature': (25, 'Room Temperature (RT, ~25°C)'),
        'rt': (25, 'Room Temperature (RT, ~25°C)'),
        'ambient': (25, 'Room Temperature (RT, ~25°C)'),
        'ambient temperature': (25, 'Room Temperature (RT, ~25°C)'),
        '室温': (25, 'Room Temperature (RT, ~25°C)'),
        '常温': (25, 'Room Temperature (RT, ~25°C)'),
        '25°c': (25, 'Room Temperature (RT, ~25°C)'),
        '25 °c': (25, 'Room Temperature (RT, ~25°C)'),
        '25℃': (25, 'Room Temperature (RT, ~25°C)'),
        'ice bath': (0, 'Ice Bath (~0°C)'),
        'ice-bath': (0, 'Ice Bath (~0°C)'),
        '冰浴': (0, 'Ice Bath (~0°C)'),
        '0°c': (0, 'Ice Bath (~0°C)'),
        '0 °c': (0, 'Ice Bath (~0°C)'),
        'reflux': (100, 'Reflux'),
        'refluxing': (100, 'Reflux'),
        '回流': (100, 'Reflux'),
        'heating': (80, 'Heating'),
        'heated': (80, 'Heating'),
        '加热': (80, 'Heating'),
        'elevated temperature': (60, 'Elevated Temperature'),
        'elevated': (60, 'Elevated Temperature'),
        'low temperature': (4, 'Low Temperature'),
        'cold': (4, 'Low Temperature'),
        'cooling': (4, 'Low Temperature'),
        'body temperature': (37, 'Body Temperature (37°C)'),
        'physiological': (37, 'Body Temperature (37°C)'),
        '37°c': (37, 'Body Temperature (37°C)'),
        '37 °c': (37, 'Body Temperature (37°C)'),
    }
    
    for key, (value, label) in temp_mappings.items():
        if key in temp_str:
            return value, label
    
    patterns = [
        r'(\d+)\s*[-–to]\s*(\d+)\s*[°℃]?\s*c',
        r'(\d+)\s*[°℃]\s*c',
        r'(\d+)\s*°\s*c',
        r'(\d+)\s*℃',
        r'(\d+)\s*c\b',
        r'(\d+)\s*k\b',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, temp_str)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                val1, val2 = int(groups[0]), int(groups[1])
                avg_val = (val1 + val2) // 2
                return avg_val, f'{val1}-{val2}°C'
            else:
                val = int(groups[0])
                if 'k' in temp_str and val > 200:
                    val = val - 273
                return val, f'{val}°C'
    
    return None, temp_str if temp_str else None


def categorize_temperature(temp_value):
    if temp_value is None:
        return 'Unknown'
    if temp_value <= 0:
        return '≤0°C (Ice Bath/Low Temp)'
    elif temp_value <= 30:
        return '0-30°C (Room Temp)'
    elif temp_value <= 60:
        return '30-60°C (Mild Heating)'
    elif temp_value <= 100:
        return '60-100°C (Medium Temp)'
    elif temp_value <= 150:
        return '100-150°C (High Temp)'
    elif temp_value <= 200:
        return '150-200°C (High Temp)'
    elif temp_value <= 300:
        return '200-300°C (Very High Temp)'
    else:
        return '>300°C (Extreme Temp)'


# ==================== 文献搜索类 ====================
class SimpleLiteratureSearcher:
    def __init__(self, max_workers: int = 5):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        self.max_workers = max_workers
    
    def get_doi_from_crossref(self, title: str) -> Optional[str]:
        url = 'https://api.crossref.org/works'
        params = {'query.title': title, 'rows': 1}
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=15)
            if response.status_code == 200:
                items = response.json().get('message', {}).get('items', [])
                if items:
                    return items[0].get('DOI')
        except:
            pass
        return None
    
    def get_abstract_from_crossref(self, doi: str) -> Optional[str]:
        url = f'https://api.crossref.org/works/{doi}'
        try:
            response = requests.get(url, headers=self.headers, timeout=15)
            if response.status_code == 200:
                data = response.json().get('message', {})
                abstract = data.get('abstract', '')
                if abstract:
                    abstract = re.sub(r'<[^>]+>', '', abstract)
                    return abstract.strip()
        except:
            pass
        return None
    
    def enrich_with_crossref(self, paper: Dict) -> Dict:
        if paper.get('abstract'):
            return paper
        
        doi = paper.get('doi')
        if not doi and paper.get('title'):
            doi = self.get_doi_from_crossref(paper['title'])
            if doi:
                paper['doi'] = doi
        
        if not doi:
            return paper
        
        try:
            url = f'https://api.crossref.org/works/{doi}'
            response = requests.get(url, headers=self.headers, timeout=15)
            if response.status_code == 200:
                data = response.json().get('message', {})
                abstract = data.get('abstract', '')
                if abstract:
                    abstract = re.sub(r'<[^>]+>', '', abstract)
                    paper['abstract'] = abstract.strip()
                    paper['abstract_source'] = 'Crossref'
        except:
            pass
        
        return paper
    
    def search_semantic_scholar(self, query: str, limit: int = 200) -> List[Dict]:
        url = 'https://api.semanticscholar.org/graph/v1/paper/search'
        all_results = []
        offset = 0
        
        while len(all_results) < limit:
            params = {
                'query': query,
                'limit': min(100, limit - len(all_results)),
                'offset': offset,
                'fields': 'title,abstract,venue,externalIds,year,authors'
            }
            
            try:
                response = requests.get(url, params=params, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    papers = response.json().get('data', [])
                    if not papers:
                        break
                    
                    for p in papers:
                        external_ids = p.get('externalIds', {})
                        all_results.append({
                            'title': p.get('title', ''),
                            'venue': p.get('venue', ''),
                            'abstract': p.get('abstract', ''),
                            'year': p.get('year', ''),
                            'authors': ', '.join([a.get('name', '') for a in p.get('authors', [])[:3]]),
                            'doi': external_ids.get('DOI', ''),
                        })
                    
                    offset += len(papers)
                    time.sleep(0.5)
                else:
                    break
            except Exception as e:
                print(f"Semantic Scholar search error: {e}")
                break
        
        return all_results
    
    def search_arxiv(self, query: str, max_results: int = 500) -> List[Dict]:
        url = 'http://export.arxiv.org/api/query'
        all_results = []
        start = 0
        batch_size = 100
        
        while len(all_results) < max_results:
            params = {
                'search_query': f'all:"{query}"',
                'start': start,
                'max_results': min(batch_size, max_results - len(all_results)),
                'sortBy': 'relevance'
            }
            
            try:
                response = requests.get(url, params=params, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    papers = self._parse_arxiv(response.text)
                    if not papers:
                        break
                    
                    all_results.extend(papers)
                    start += len(papers)
                    time.sleep(1)
                else:
                    break
            except Exception as e:
                print(f"arXiv search error: {e}")
                break
        
        return all_results
    
    def _parse_arxiv(self, xml_content: str) -> List[Dict]:
        try:
            root = ET.fromstring(xml_content)
        except:
            return []
        
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        papers = []
        
        for entry in root.findall('atom:entry', ns):
            title_elem = entry.find('atom:title', ns)
            summary_elem = entry.find('atom:summary', ns)
            doi_elem = entry.find('atom:id', ns)
            published_elem = entry.find('atom:published', ns)
            
            arxiv_id = ''
            if doi_elem is not None:
                arxiv_id = doi_elem.text.split('/')[-1]
            
            year = ''
            if published_elem is not None:
                year = published_elem.text[:4]
            
            authors = []
            for author in entry.findall('atom:author', ns):
                name_elem = author.find('atom:name', ns)
                if name_elem is not None:
                    authors.append(name_elem.text)
            
            papers.append({
                'title': title_elem.text.strip() if title_elem is not None else '',
                'venue': 'arXiv',
                'abstract': summary_elem.text.strip() if summary_elem is not None else '',
                'doi': f'arXiv:{arxiv_id}' if arxiv_id else '',
                'year': year,
                'authors': ', '.join(authors[:3])
            })
        
        return papers
    
    def search_openalex(self, query: str, per_page: int = 500) -> List[Dict]:
        url = 'https://api.openalex.org/works'
        all_results = []
        page = 1
        
        while len(all_results) < per_page:
            params = {
                'search': query,
                'per-page': min(200, per_page - len(all_results)),
                'page': page
            }
            
            try:
                response = requests.get(url, params=params, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    results = response.json().get('results', [])
                    if not results:
                        break
                    
                    for r in results:
                        venue = ''
                        primary_location = r.get('primary_location', {})
                        if primary_location:
                            source = primary_location.get('source', {})
                            if source:
                                venue = source.get('display_name', '')
                        
                        authors = ', '.join([a.get('author', {}).get('display_name', '') 
                                           for a in r.get('authorships', [])[:3]])
                        
                        all_results.append({
                            'title': r.get('title', ''),
                            'venue': venue,
                            'abstract': r.get('abstract', ''),
                            'doi': r.get('doi', '').replace('https://doi.org/', '') if r.get('doi') else '',
                            'year': r.get('publication_year', ''),
                            'authors': authors
                        })
                    
                    page += 1
                    time.sleep(0.5)
                else:
                    break
            except Exception as e:
                print(f"OpenAlex search error: {e}")
                break
        
        return all_results
    
    def search_pubmed(self, query: str, max_results: int = 500) -> List[Dict]:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        search_params = {
            'db': 'pubmed',
            'term': query,
            'retmax': max_results,
            'retmode': 'json'
        }
        
        try:
            search_response = requests.get(search_url, params=search_params, headers=self.headers, timeout=30)
            if search_response.status_code == 200:
                id_list = search_response.json().get('esearchresult', {}).get('idlist', [])
                
                if not id_list:
                    return []
                
                all_papers = []
                batch_size = 200
                
                for i in range(0, len(id_list), batch_size):
                    batch_ids = id_list[i:i+batch_size]
                    
                    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                    fetch_params = {
                        'db': 'pubmed',
                        'id': ','.join(batch_ids),
                        'retmode': 'xml'
                    }
                    
                    fetch_response = requests.get(fetch_url, params=fetch_params, headers=self.headers, timeout=30)
                    if fetch_response.status_code == 200:
                        papers = self._parse_pubmed(fetch_response.text)
                        all_papers.extend(papers)
                    
                    time.sleep(0.5)
                
                return all_papers
        except Exception as e:
            print(f"PubMed search error: {e}")
        return []
    
    def _parse_pubmed(self, xml_content: str) -> List[Dict]:
        try:
            root = ET.fromstring(xml_content)
        except:
            return []
        
        papers = []
        for article in root.findall('.//PubmedArticle'):
            title_elem = article.find('.//ArticleTitle')
            journal_elem = article.find('.//Journal/Title')
            abstract_elem = article.find('.//AbstractText')
            year_elem = article.find('.//PubDate/Year')
            
            doi = ''
            for article_id in article.findall('.//ArticleId'):
                if article_id.get('IdType') == 'doi':
                    doi = article_id.text
                    break
            
            authors = []
            for author in article.findall('.//Author')[:3]:
                last_name = author.find('.//LastName')
                fore_name = author.find('.//ForeName')
                if last_name is not None:
                    name = last_name.text
                    if fore_name is not None:
                        name = f"{fore_name.text} {name}"
                    authors.append(name)
            
            papers.append({
                'title': title_elem.text if title_elem is not None else '',
                'venue': journal_elem.text if journal_elem is not None else '',
                'abstract': abstract_elem.text if abstract_elem is not None else '',
                'doi': doi,
                'year': year_elem.text if year_elem is not None else '',
                'authors': ', '.join(authors)
            })
        
        return papers
    
    def search_all(self, query: str, sources: List[str] = None, max_papers: int = 500) -> List[Dict]:
        if sources is None:
            sources = ['semantic_scholar', 'pubmed', 'openalex', 'arxiv']
        
        all_papers = []
        
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(sources))) as executor:
            futures = {}
            
            for source in sources:
                if source == 'semantic_scholar':
                    futures[executor.submit(self.search_semantic_scholar, query, max_papers)] = source
                elif source == 'arxiv':
                    futures[executor.submit(self.search_arxiv, query, max_papers)] = source
                elif source == 'openalex':
                    futures[executor.submit(self.search_openalex, query, max_papers)] = source
                elif source == 'pubmed':
                    futures[executor.submit(self.search_pubmed, query, max_papers)] = source
            
            for future in as_completed(futures):
                source = futures[future]
                try:
                    papers = future.result(timeout=120)
                    all_papers.extend(papers)
                    print(f"  ✓ {source}: found {len(papers)} papers")
                except Exception as e:
                    print(f"  ✗ {source}: failed")
        
        return all_papers
    
    def deduplicate(self, papers: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        
        for paper in papers:
            if not paper or not paper.get('title'):
                continue
            
            title = paper['title'].lower().strip()
            if title and title not in seen:
                seen.add(title)
                unique.append(paper)
        
        return unique
    
    def enrich_batch_with_crossref(self, papers: List[Dict], max_workers: int = 5) -> List[Dict]:
        print("\nUsing Crossref to supplement abstracts...")
        enriched = []
        
        papers_to_enrich = [p for p in papers if not p.get('abstract')]
        papers_with_abstract = [p for p in papers if p.get('abstract')]
        
        print(f"  Need to process: {len(papers_to_enrich)} papers without abstract")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.enrich_with_crossref, paper): paper 
                      for paper in papers_to_enrich}
            
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    enriched_paper = future.result(timeout=20)
                    enriched.append(enriched_paper)
                    if i % 10 == 0:
                        print(f"  Progress: {i}/{len(futures)}")
                except Exception as e:
                    enriched.append(futures[future])
        
        enriched.extend(papers_with_abstract)
        
        newly_found = sum(1 for p in enriched if p.get('abstract') and p.get('abstract_source'))
        print(f"   Newly obtained abstracts: {newly_found}")
        
        return enriched


# ==================== 聚合物信息提取类 ====================
class PolymerExtractor:
    def __init__(self, api_key: str, base_url: str, model_name: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model_name = model_name
        
        try:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url
            )
        except Exception as e:
            print(f"OpenAI client initialization failed: {e}")
            raise
        
        self.results = []
    
    def create_extraction_prompt(self, title: str, abstract: str, mode: str) -> str:
        if mode == "synthesis":
            prompt = f"""You are an expert polymer chemist. Analyze this paper abstract and extract COMPLETE polymerization reaction information.

**Paper Title**: {title}

**Paper Abstract**: {abstract}

---

**INSTRUCTIONS**:

1. First, determine if there are ANY polymerization reactions in the abstract.
   - If NO polymerization reactions exist, respond with: {{"has_polymerization": false, "total_reactions": 0, "reactions": []}}
   - If polymerization reactions exist, continue to step 2.

2. Count how many DIFFERENT polymerization reactions are described.

3. For EACH reaction, extract the following information:

**CRITICAL RULES**:
- Extract ONLY information EXPLICITLY stated in the abstract
- For reactant names, use FULL CHEMICAL NAMES, NOT abbreviations
- If information is not mentioned, use null
- Do NOT invent or assume any information

**IMPORTANT FOR TEMPERATURE**:
- Extract temperature even if expressed in TEXT form, such as:
  - "room temperature", "RT", "ambient temperature" → record as "room temperature" or "RT"
  - "reflux", "refluxing" → record as "reflux"
  - "ice bath", "0°C" → record as "ice bath" or "0°C"
  - "heated", "heating" → record as "heating"
  - "elevated temperature" → record as "elevated temperature"
  - Any numeric temperature like "280°C", "100-150°C" → record exactly as stated
- DO NOT leave temperature as null if ANY temperature indication is present

**Required JSON format**:

{{
  "has_polymerization": true or false,
  "total_reactions": number,
  "reactions": [
    {{
      "reaction_number": integer,
      "reaction_type": "type of polymerization or null",
      "reactants": ["full chemical name 1", "full chemical name 2", ...] or null,
      "product_name": "full product name or null",
      "product_abbreviation": "abbreviation or null",
      "properties": [
        {{
          "property_name": "property name (e.g., Mn, Mw, Tg, Tm, PDI)",
          "value": "numeric value OR qualitative description",
          "unit": "unit (if numeric value) or null",
          "measurement_condition": "condition or null"
        }}
      ] or null,
      "reaction_conditions": {{
        "temperature": "MUST extract if mentioned - can be numeric (280°C) OR text (room temperature, RT, reflux, ice bath, heating, etc.) or null if truly not mentioned",
        "time": "value with unit or null",
        "catalyst": "name or null",
        "solvent": "name or null",
        "atmosphere": "name (nitrogen, argon, air, vacuum, etc.) or null",
        "pressure": "value with unit or null",
        "initiator": "name or null",
        "other": "other conditions or null"
      }}
    }}
  ]
}}

Return ONLY valid JSON. No explanations. No markdown. No code blocks."""
        else:
            prompt = f"""You are an expert polymer chemist. Analyze this paper abstract and extract property-condition relationships.

**Title**: {title}
**Abstract**: {abstract}

**Required JSON format**:
{{
  "has_polymer": true,
  "total_data_points": number,
  "data_points": [
    {{
      "polymer_type": "type of polymer",
      "polymer_name": "full name",
      "condition_name": "condition name",
      "condition_value": "value with unit",
      "property_name": "property name",
      "property_value": "value with unit",
      "relationship": "direct/inverse/optimal"
    }}
  ]
}}

Return ONLY valid JSON."""
        
        return prompt
    
    def extract_from_abstract(self, title: str, abstract: str, paper_id: str, mode: str):
        try:
            prompt = self.create_extraction_prompt(title, abstract, mode)
            
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are a polymer chemistry expert. Respond only with valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                max_tokens=4000
            )
            
            result_text = response.choices[0].message.content.strip()
            
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            
            result = json.loads(result_text.strip())
            result['paper_id'] = paper_id
            result['title'] = title
            
            return result
            
        except Exception as e:
            print(f"API call error: {e}")
            return {
                'paper_id': paper_id,
                'title': title,
                'has_polymerization': False,
                'error': str(e)
            }
    
    def process_papers(self, papers: List[Dict], mode: str = "synthesis", delay: float = 0.5) -> List[Dict]:
        """处理论文列表，提取信息"""
        self.results = []
        total = len(papers)
        
        print(f"  Processing {total} papers...")
        
        for i, paper in enumerate(papers):
            title = paper.get('title', '')
            abstract = paper.get('abstract', '')
            paper_id = paper.get('doi', f"paper_{i}")
            
            if not abstract:
                print(f"    [{i+1}/{total}] Skipping {title[:50]}... (no abstract)")
                continue
            
            print(f"    [{i+1}/{total}] Processing: {title[:50]}...")
            
            result = self.extract_from_abstract(title, abstract, paper_id, mode)
            self.results.append(result)
            
            if delay > 0 and i < total - 1:
                time.sleep(delay)
        
        successful = sum(1 for r in self.results if r.get('has_polymerization'))
        print(f"  Successfully processed: {successful}/{len(self.results)} papers")
        
        return self.results

    def convert_to_dataframe(self):
        """转换为DataFrame - 与mycode.py保持一致，包含完整字段"""
        rows = []
        
        for result in self.results:
            if not result.get('has_polymerization') or 'reactions' not in result:
                continue
            
            for reaction in result.get('reactions', []):
                row = {
                    'paper_id': result.get('paper_id'),
                    'paper_title': result.get('title'),
                    'reaction_number': reaction.get('reaction_number'),
                    'reaction_type': reaction.get('reaction_type')
                }
                
                # 处理反应物 - 支持多个反应物
                reactants = reaction.get('reactants', [])
                if reactants and isinstance(reactants, list):
                    for i, reactant in enumerate(reactants, 1):
                        row[f'reactant_{i}'] = reactant
                
                # 产物信息
                row['product_name'] = reaction.get('product_name')
                row['product_abbreviation'] = reaction.get('product_abbreviation')
                
                # 处理性质信息
                properties = reaction.get('properties')
                if properties and isinstance(properties, list):
                    for i, prop in enumerate(properties, 1):
                        if isinstance(prop, dict):
                            row[f'property_name_{i}'] = prop.get('property_name')
                            row[f'property_value_{i}'] = prop.get('value')
                            row[f'property_unit_{i}'] = prop.get('unit')
                            if prop.get('measurement_condition'):
                                row[f'property_condition_{i}'] = prop.get('measurement_condition')
                
                # 处理反应条件
                conditions = reaction.get('reaction_conditions', {})
                if conditions and isinstance(conditions, dict):
                    row['temperature'] = conditions.get('temperature')
                    row['time'] = conditions.get('time')
                    row['catalyst'] = conditions.get('catalyst')
                    row['solvent'] = conditions.get('solvent')
                    row['atmosphere'] = conditions.get('atmosphere')
                    row['pressure'] = conditions.get('pressure')
                    row['initiator'] = conditions.get('initiator')
                    row['other_conditions'] = conditions.get('other')
                
                rows.append(row)
        
        if not rows:
            return pd.DataFrame()
        
        df = pd.DataFrame(rows)
        
        # 重新排列列顺序，与mycode.py保持一致
        base_cols = ['paper_id', 'paper_title', 'reaction_number', 'reaction_type']
        
        # 反应物列
        reactant_cols = sorted([col for col in df.columns if col.startswith('reactant_')], 
                               key=lambda x: int(x.split('_')[1]) if x.split('_')[1].isdigit() else 0)
        
        product_cols = ['product_name', 'product_abbreviation']
        
        # 性质列
        property_cols = []
        property_nums = set()
        for col in df.columns:
            if col.startswith('property_'):
                parts = col.split('_')
                if len(parts) >= 3 and parts[-1].isdigit():
                    property_nums.add(int(parts[-1]))
        
        for i in sorted(property_nums):
            property_cols.extend([
                f'property_name_{i}',
                f'property_value_{i}',
                f'property_unit_{i}',
                f'property_condition_{i}'
            ])
        
        property_cols = [col for col in property_cols if col in df.columns]
        
        # 条件列
        condition_cols = ['temperature', 'time', 'catalyst', 'solvent', 'atmosphere', 
                          'pressure', 'initiator', 'other_conditions']
        condition_cols = [col for col in condition_cols if col in df.columns]
        
        # 组合所有列
        ordered_cols = base_cols + reactant_cols + product_cols + property_cols + condition_cols
        ordered_cols = [col for col in ordered_cols if col in df.columns]
        
        # 添加其他可能存在的列
        other_cols = [col for col in df.columns if col not in ordered_cols]
        final_cols = ordered_cols + other_cols
        
        df = df[final_cols]


        cols = df.columns.tolist()
        if 'paper_id' in cols:
            cols.remove('paper_id')
            cols.insert(0, 'paper_id')
            df = df[cols] 
            
        return df


# ==================== 图表生成函数（增强灵敏性） ====================
def generate_temperature_pie(df):
    """生成温度分布饼图 - 只有足够数据时才生成"""
    if df.empty or 'temperature' not in df.columns:
        return None, None
    
    # 处理温度数据
    temp_values = []
    temp_labels = []
    temp_categories = []
    
    for temp in df['temperature']:
        value, label = standardize_temperature(temp)
        temp_values.append(value)
        temp_labels.append(label)
        temp_categories.append(categorize_temperature(value))
    
    df['temp_value'] = temp_values
    df['temp_label'] = temp_labels
    df['temp_category'] = temp_categories
    
    labeled_temps = sum(1 for l in temp_labels if l is not None)
    
    # 如果有效温度数据少于1条，不生成图表
    if labeled_temps < 1:
        print(f"Temperature data insufficient: {labeled_temps} records (<1), skipping chart")
        return None, df
    
    # 检查是否有足够的分类数据
    if 'temp_category' in df.columns:
        category_counts = df['temp_category'].value_counts()
        # 过滤掉Unknown
        valid_categories = {k: v for k, v in category_counts.items() if k != 'Unknown'}
        
        # 如果有效分类少于1个，不生成图表
        if len(valid_categories) < 1:
            print(f"Valid temperature categories insufficient: {len(valid_categories)} (<1), skipping chart")
            return None, df
        
        # 如果总数据量少于1条，不生成图表
        if sum(valid_categories.values()) < 1:
            print(f"Total temperature data insufficient: {sum(valid_categories.values())} (<1), skipping chart")
            return None, df
    
    fig, ax = plt.subplots(figsize=(10, 8))
    
    if 'temp_category' in df.columns:
        category_order = [
            '≤0°C (Ice Bath/Low Temp)',
            '0-30°C (Room Temp)',
            '30-60°C (Mild Heating)',
            '60-100°C (Medium Temp)',
            '100-150°C (High Temp)',
            '150-200°C (High Temp)',
            '200-300°C (Very High Temp)',
            '>300°C (Extreme Temp)',
            'Unknown'
        ]
        
        category_counts = df['temp_category'].value_counts()
        
        ordered_cats = []
        ordered_counts = []
        for cat in category_order:
            if cat in category_counts.index and cat != 'Unknown':  # 跳过Unknown
                ordered_cats.append(cat)
                ordered_counts.append(category_counts[cat])
        
        # 再次检查有效数据
        if len(ordered_cats) < 1 or sum(ordered_counts) < 1:
            print(f"Insufficient valid temperature data after filtering: {len(ordered_cats)} categories, {sum(ordered_counts)} records")
            plt.close()
            return None, df
        
        if ordered_counts:
            # 简化标签用于饼图显示
            display_cats = []
            for cat in ordered_cats:
                short_cat = cat.replace(' (Ice Bath/Low Temp)', '').replace(' (Room Temp)', '').replace(' (Mild Heating)', '').replace(' (Medium Temp)', '').replace(' (High Temp)', '').replace(' (Very High Temp)', '').replace(' (Extreme Temp)', '')
                display_cats.append(short_cat)
            
            # 使用更美观的颜色
            colors = plt.cm.RdYlBu_r(np.linspace(0.2, 0.8, len(display_cats)))
            
            # 绘制饼图
            wedges, texts, autotexts = ax.pie(
                ordered_counts, 
                labels=display_cats, 
                autopct=lambda pct: f'{pct:.1f}%\n({int(pct/100*sum(ordered_counts))})',
                colors=colors,
                startangle=90,
                textprops={'fontsize': 9}
            )
            
            # 美化百分比文字
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontweight('bold')
                autotext.set_fontsize(8)
            
            # 美化标签
            for text in texts:
                text.set_fontsize(9)
                text.set_fontweight('medium')
            
            ax.set_title(f'Temperature Distribution\n(Recognized {sum(ordered_counts)} records)', fontweight='bold', fontsize=12, pad=20)
    
    plt.tight_layout()
    
    # 转换为base64
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='white')
    buf.seek(0)
    img_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    plt.close()
    
    return img_base64, df


def generate_solvent_pie(df):
    """生成溶剂饼图 - 只有足够数据时才生成"""
    if df.empty or 'solvent' not in df.columns:
        return None
    
    solvents = df['solvent'].dropna()
    
    # 如果溶剂数据少于1条，不生成图表
    if len(solvents) < 1:
        print(f"Solvent data insufficient: {len(solvents)} records (<1), skipping chart")
        return None
    
    solvent_counts = solvents.value_counts()
    
    # 如果不同溶剂种类少于2种，不生成图表
    if len(solvent_counts) < 1:
        print(f"Solvent variety insufficient: {len(solvent_counts)} types (<1), skipping chart")
        return None
    
    # 只显示前6种，但如果只有2-3种，就全部显示
    display_count = min(6, len(solvent_counts))
    solvent_counts = solvent_counts.head(display_count)
    
    fig, ax = plt.subplots(figsize=(8, 6))
    colors = ['#4472C4', '#ED7D31', '#A5A5A5', '#FFC000', '#5B9BD5', '#70AD47']
    
    wedges, texts, autotexts = ax.pie(solvent_counts.values, 
                                       labels=solvent_counts.index, 
                                       autopct='%1.1f%%',
                                       colors=colors[:len(solvent_counts)], 
                                       startangle=90,
                                       textprops={'fontsize': 9})
    
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_fontweight('bold')
        autotext.set_fontsize(8)
    
    ax.set_title(f'Common Solvents\n(Total {len(solvents)} records)', fontweight='bold', fontsize=12)
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='white')
    buf.seek(0)
    img_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    plt.close()
    
    return img_base64


def generate_reaction_type_plot(df):
    """生成反应类型分布图 - 只有足够数据时才生成"""
    if df.empty or 'reaction_type' not in df.columns:
        return None, None
    
    reaction_types = df['reaction_type'].dropna()
    
    # 如果反应类型数据少于1条，不生成图表
    if len(reaction_types) < 1:
        print(f"Reaction type data insufficient: {len(reaction_types)} records (<1), skipping chart")
        return None, None
    
    rt_counts = reaction_types.value_counts()
    
    # 如果不同反应类型少于1种，不生成图表
    if len(rt_counts) < 1:
        print(f"Reaction type variety insufficient: {len(rt_counts)} types (<1), skipping chart")
        return None, None
    
    # 只显示前8种
    rt_counts = rt_counts.head(8)
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    y_pos = range(len(rt_counts))
    colors = plt.cm.Pastel1(np.linspace(0, 1, len(rt_counts)))
    
    bars = ax.barh(y_pos, rt_counts.values, color=colors, edgecolor='black', linewidth=0.5)
    ax.set_yticks(y_pos)
    labels = [str(l)[:30] + '...' if len(str(l)) > 30 else str(l) for l in rt_counts.index]
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel('Count', fontweight='bold', fontsize=10)
    ax.set_title('Reaction Types', fontweight='bold', fontsize=11)
    ax.invert_yaxis()
    
    for bar in bars:
        width = bar.get_width()
        if width > 0:
            ax.text(width + 0.5, bar.get_y() + bar.get_height()/2.,
                    f'{int(width)}', ha='left', va='center', fontsize=8)
    
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='white')
    buf.seek(0)
    img_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    plt.close()
    
    return img_base64, rt_counts


def generate_catalyst_table(df):
    """生成催化剂统计表 - 只有足够数据时才生成"""
    if df.empty or 'catalyst' not in df.columns:
        return []
    
    catalysts = df['catalyst'].dropna()
    
    # 如果催化剂数据少于1条，返回空列表
    if len(catalysts) < 1:
        print(f"Catalyst data insufficient: {len(catalysts)} records (<1), skipping table")
        return []
    
    catalyst_counts = catalysts.value_counts()
    
    # 如果不同催化剂种类少于1种，返回空列表
    if len(catalyst_counts) < 1:
        return []
    
    catalyst_counts = catalyst_counts.head(8)
    
    result = []
    for cat, count in catalyst_counts.items():
        cat_display = str(cat)[:40] + '...' if len(str(cat)) > 40 else str(cat)
        result.append({'name': cat_display, 'count': int(count)})
    
    return result


def get_temperature_labels(df):
    """获取温度标签详情 - 只有足够数据时才返回"""
    if df.empty or 'temperature' not in df.columns:
        return []
    
    temp_labels = []
    for temp in df['temperature']:
        value, label = standardize_temperature(temp)
        temp_labels.append(label)
    
    df['temp_label'] = temp_labels
    
    label_counts = pd.Series(temp_labels).dropna().value_counts()
    
    # 如果有效温度标签少于1个，返回空列表
    if len(label_counts) < 1:
        print(f"Temperature labels insufficient: {len(label_counts)} labels (<1), skipping table")
        return []
    
    label_counts = label_counts.head(10)
    
    result = []
    for label, count in label_counts.items():
        label_display = str(label)[:35] + '...' if len(str(label)) > 35 else str(label)
        result.append({'label': label_display, 'count': int(count)})
    
    return result


def get_condition_summary(df):
    """获取其他条件汇总"""
    summary = []
    
    if not df.empty:
        if 'time' in df.columns:
            times = df['time'].dropna()
            if len(times) > 0:
                summary.append(f" Reaction time: {len(times)} records")
                most_common_time = times.value_counts().index[0] if len(times) > 0 else 'N/A'
                summary.append(f"   Most common: {most_common_time}")
        
        if 'atmosphere' in df.columns:
            atm = df['atmosphere'].dropna()
            if len(atm) > 0:
                summary.append(f"Atmosphere: {len(atm)} records")
                most_common_atm = atm.value_counts().index[0] if len(atm) > 0 else 'N/A'
                summary.append(f"   Most common: {most_common_atm}")
        
        if 'pressure' in df.columns:
            pressure = df['pressure'].dropna()
            if len(pressure) > 0:
                summary.append(f" Pressure: {len(pressure)} records")
        
        if 'initiator' in df.columns:
            init = df['initiator'].dropna()
            if len(init) > 0:
                summary.append(f" Initiator: {len(init)} records")
    
    if not summary:
        summary.append("Limited condition data available")
    
    return summary


# ==================== 示例数据生成 ====================
def create_example_data(material: str, mode: str = "synthesis"):
    material_lower = material.lower()
    
    examples = {
        'polyethylene': {
            'title': 'Synthesis of Polyethylene',
            'abstract': 'Polyethylene was synthesized via coordination polymerization using Ziegler-Natta catalyst at 80°C and 5 bar pressure. The polymerization was carried out in hexane solvent for 2 hours. The resulting polymer had Mw = 150,000 g/mol and melting point 135°C.'
        },
        'polyimide': {
            'title': 'Synthesis of Aromatic Polyimide',
            'abstract': 'Aromatic polyimide was synthesized from pyromellitic dianhydride (PMDA) and 4,4\'-oxydianiline (ODA) in DMAc solvent. The poly(amic acid) intermediate was thermally imidized at 300°C for 2 hours. The resulting polyimide showed Tg > 400°C and excellent thermal stability.'
        },
        'nylon': {
            'title': 'Nylon 6 Synthesis',
            'abstract': 'Nylon 6 was prepared by ring-opening polymerization of ε-caprolactam at 250°C using 6-aminocaproic acid (2 wt%) as initiator. The reaction was carried out under nitrogen atmosphere for 6 hours.'
        },
        'pla': {
            'title': 'PLA Synthesis',
            'abstract': 'Poly(L-lactic acid) (PLA) was synthesized via ring-opening polymerization of L-lactide using tin(II) 2-ethylhexanoate (0.1 mol%) as catalyst and benzyl alcohol as initiator. Polymerization was conducted in toluene at 130°C for 24 hours under argon.'
        },
        'pet': {
            'title': 'PET Synthesis',
            'abstract': 'Poly(ethylene terephthalate) (PET) was synthesized by melt polycondensation of terephthalic acid (1.0 mol) and ethylene glycol (1.2 mol) using antimony trioxide (0.05 wt%) as catalyst. The reaction was carried out at 280°C for 3 hours under nitrogen.'
        }
    }
    
    for key, example in examples.items():
        if key in material_lower:
            return [{
                'title': example['title'],
                'abstract': example['abstract'],
                'doi': f'example_{key}'
            }]
    
    return [{
        'title': f'Synthesis of {material}',
        'abstract': f'{material} was synthesized via polymerization reaction. The reaction was carried out at 200°C using appropriate catalyst in solvent. The polymer showed good thermal stability and mechanical properties.',
        'doi': 'example_general'
    }]


# ==================== API 路由 ====================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


# ==================== CSV Export Endpoint ====================
@app.route('/api/export_csv', methods=['POST'])
def export_csv():
    """Export data to CSV file"""
    try:
        data = request.json
        df_data = data.get('data', [])
        filename = data.get('filename', f'synthesis_results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
        
        if not df_data:
            return jsonify({'error': 'No data to export'})
        
        df = pd.DataFrame(df_data)
        csv_content = df.to_csv(index=False, encoding='utf-8-sig')
        
        return jsonify({
            'success': True,
            'csv_content': csv_content,
            'filename': filename
        })
        
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/history/<int:index>', methods=['DELETE'])
def delete_history_item(index):
    """删除单条历史记录"""
    try:
        history = load_history()
        if 0 <= index < len(history):
            deleted = history.pop(index)
            save_history(history)
            return jsonify({'success': True, 'deleted': deleted})
        return jsonify({'success': False, 'error': 'Index out of range'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/search', methods=['POST'])
def search():
    print("=" * 50)
    print("Received POST request to /api/search")
    print(f"Request headers: {dict(request.headers)}")
    try:
        data = request.json
        material = data.get('material')
        mode = data.get('mode', 'synthesis')
        model = data.get('model')
        api_key = data.get('api_key')
        base_url = data.get('base_url')
        max_papers = data.get('max_papers', 500)  # 获取用户自定义的文献数量
        
        if not material:
            return jsonify({'error': 'Please provide a material name'})
        
        if not api_key:
            return jsonify({'error': 'Please provide API Key'})
        
        if not base_url:
            return jsonify({'error': 'Please provide Base URL'})
        
        if not model:
            return jsonify({'error': 'Please provide Model Name'})
        
        # 验证并限制max_papers范围
        try:
            max_papers = int(max_papers)
            if max_papers < 1:
                max_papers = 50
            elif max_papers > 2000:
                max_papers = 2000  # 设置最大限制，避免过度请求
        except:
            max_papers = 500
        
        print(f"=" * 50)
        print(f"Searching: {material}")
        print(f"Mode: {mode}, Model: {model}")
        print(f"Max papers (user custom): {max_papers}")
        print(f"API Base URL: {base_url}")
        print(f"=" * 50)
        
        print("\n[Step 1] Online literature search...")
        searcher = SimpleLiteratureSearcher(max_workers=4)
        
        raw_papers = searcher.search_all(material, max_papers=max_papers)
        papers = searcher.deduplicate(raw_papers)
        papers = searcher.enrich_batch_with_crossref(papers)
        papers = [p for p in papers if p.get('abstract')]
        print(f"  Found {len(papers)} papers with abstracts")
        
        if len(papers) < 1:
            print("  No relevant literature found, using sample data...")
            papers = create_example_data(material, mode)
            example_used = True
        else:
            example_used = False
        
        print("\n[Step 2] Initializing AI extractor...")
        extractor = PolymerExtractor(api_key, base_url, model)
        
        print("\n[Step 3] Extracting synthesis information...")
        results = extractor.process_papers(
            papers,
            mode=mode,
            delay=0.5
        )
        
        df = extractor.convert_to_dataframe()
        print(f"  Extracted {len(df)} reaction records")
        
        # 统计数据 - 增加更多统计项
        stats = {
            'totalPapers': len(papers),
            'withSynthesis': len(df),
            'avgReliability': 78,
            'exampleUsed': example_used,
            'hasReactants': len(df[df['reactant_1'].notna()]) if 'reactant_1' in df.columns else 0,
            'hasTemperature': len(df[df['temperature'].notna()]) if 'temperature' in df.columns else 0,
            'hasCatalyst': len(df[df['catalyst'].notna()]) if 'catalyst' in df.columns else 0,
            'hasTime': len(df[df['time'].notna()]) if 'time' in df.columns else 0,
            'hasAtmosphere': len(df[df['atmosphere'].notna()]) if 'atmosphere' in df.columns else 0,
            'hasPressure': len(df[df['pressure'].notna()]) if 'pressure' in df.columns else 0,
            'hasInitiator': len(df[df['initiator'].notna()]) if 'initiator' in df.columns else 0
        }
        
        # 生成图表 - 温度分布改为饼图（增强灵敏性）
        temp_plot_img, processed_df = generate_temperature_pie(df)
        solvent_pie_img = generate_solvent_pie(df)
        reaction_plot_img, reaction_counts = generate_reaction_type_plot(df)
        catalyst_table = generate_catalyst_table(df)
        temp_labels = get_temperature_labels(df)
        condition_summary = get_condition_summary(df)
        
        # 合成方法列表 - 增加更多字段，与mycode.py保持一致
        syntheses = []
        for _, row in df.head(15).iterrows():
            # 处理反应物
            reactants_list = []
            for i in range(1, 10):  # 最多支持9个反应物
                reactant_col = f'reactant_{i}'
                if reactant_col in df.columns and pd.notna(row.get(reactant_col)):
                    reactants_list.append(str(row.get(reactant_col)))
            
            reactants_str = ', '.join(reactants_list) if reactants_list else 'Not Provided'
            
            # 处理性质信息
            properties_list = []
            for i in range(1, 5):  # 最多支持4个性质
                prop_name_col = f'property_name_{i}'
                prop_value_col = f'property_value_{i}'
                prop_unit_col = f'property_unit_{i}'
                
                if prop_name_col in df.columns and pd.notna(row.get(prop_name_col)):
                    prop_name = row.get(prop_name_col)
                    prop_value = row.get(prop_value_col) if prop_value_col in df.columns and pd.notna(row.get(prop_value_col)) else ''
                    prop_unit = row.get(prop_unit_col) if prop_unit_col in df.columns and pd.notna(row.get(prop_unit_col)) else ''
                    
                    if prop_value:
                        prop_str = f"{prop_name}: {prop_value} {prop_unit}".strip()
                    else:
                        prop_str = prop_name
                    properties_list.append(prop_str)
            
            properties_str = '; '.join(properties_list) if properties_list else 'Not Provided'
            
            syntheses.append({
                'method': row.get('reaction_type', 'Polymerization') if pd.notna(row.get('reaction_type')) else 'Polymerization',
                'reaction_type': row.get('reaction_type', 'N/A') if pd.notna(row.get('reaction_type')) else 'N/A',
                'product_name': row.get('product_name', 'N/A') if pd.notna(row.get('product_name')) else 'N/A',
                'product_abbreviation': row.get('product_abbreviation', 'N/A') if pd.notna(row.get('product_abbreviation')) else 'N/A',
                'temperature': row.get('temperature', 'Not Provided') if pd.notna(row.get('temperature')) else 'Not Provided',
                'catalyst': row.get('catalyst', 'Not Provided') if pd.notna(row.get('catalyst')) else 'Not Provided',
                'solvent': row.get('solvent', 'Not Provided') if pd.notna(row.get('solvent')) else 'Not Provided',
                'time': row.get('time', 'Not Provided') if pd.notna(row.get('time')) else 'Not Provided',
                'atmosphere': row.get('atmosphere', 'Not Provided') if pd.notna(row.get('atmosphere')) else 'Not Provided',
                'pressure': row.get('pressure', 'Not Provided') if pd.notna(row.get('pressure')) else 'Not Provided',
                'initiator': row.get('initiator', 'Not Provided') if pd.notna(row.get('initiator')) else 'Not Provided',
                'reactants': reactants_str,
                'properties': properties_str
            })
        
        # 反应类型统计表
        reaction_type_table = []
        if reaction_counts is not None and len(reaction_counts) > 0:
            total = len(df)
            for rt, count in reaction_counts.items():
                reaction_type_table.append({
                    'type': rt if rt else 'Not specified',
                    'count': int(count),
                    'percentage': round(count / total * 100, 1)
                })
        
        # 【修改】添加 dataframe 数据用于 CSV 导出，并处理 NaN 值
        if not df.empty:
            # 将所有 NaN 替换为 None（JSON 中变成 null）
            df_clean = df.replace({np.nan: None})
            dataframe_records = df_clean.to_dict('records')
        else:
            dataframe_records = []
        
        # 准备返回数据
        response_data = {
            'stats': stats,
            'syntheses': syntheses,
            'material': material,
            'mode': mode,
            'totalPapers': len(papers),
            'max_papers': max_papers,
            'exampleUsed': example_used,
            'tempPlot': temp_plot_img,
            'solventPie': solvent_pie_img,
            'reactionPlot': reaction_plot_img,
            'catalystTable': catalyst_table,
            'tempLabels': temp_labels,
            'conditionSummary': condition_summary,
            'reactionTypeTable': reaction_type_table,
            'dataframe': dataframe_records
        }
        
        # 保存搜索结果到历史记录
        save_search_result(material, mode, response_data)
        
        # 保存搜索历史（轻量级记录）
       
        
        return jsonify(response_data)
        
    except Exception as e:
        import traceback
        print(f"Error details: {traceback.format_exc()}")
        return jsonify({'error': f'Analysis failed: {str(e)}'})


@app.route('/api/history', methods=['GET'])
def get_history():
    """获取搜索历史"""
    history = load_history()
    return jsonify({'history': history})

@app.route('/api/history/clear', methods=['POST'])
def clear_history():
    """清除搜索历史"""
    save_history([])
    return jsonify({'success': True})


# ==================== HTML 模板 ====================
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PolyMind - Polymer Knowledge Retrieval System</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Segoe UI', 'Roboto', 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 50%, #ddd6fe 100%);
            min-height: 100vh;
        }
        
        .particles {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: 0;
            pointer-events: none;
        }
        
        .particle {
            position: absolute;
            background: rgba(56, 189, 248, 0.15);
            border-radius: 50%;
            animation: float 15s infinite;
        }
        
        @keyframes float {
            0%, 100% { transform: translateY(0) translateX(0); opacity: 0.3; }
            50% { transform: translateY(-80px) translateX(40px); opacity: 0.8; }
        }
        
        nav {
            background: rgba(255, 255, 255, 0.92);
            backdrop-filter: blur(12px);
            padding: 16px 48px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(56, 189, 248, 0.3);
            box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
            position: relative;
            z-index: 100;
            flex-wrap: wrap;
            gap: 16px;
        }
        
        .logo-area {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        
        .logo-area img {
            height: 42px;
            width: auto;
            object-fit: contain;
        }
        
        .logo-area span {
            font-size: 26px;
            font-weight: 800;
            background: linear-gradient(135deg, #38bdf8, #8b5cf6);
            background-clip: text;
            -webkit-background-clip: text;
            color: transparent;
        }
        
        .history-toggle {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        
        .history-btn {
            padding: 8px 20px;
            background: linear-gradient(135deg, #38bdf8, #8b5cf6);
            border: none;
            border-radius: 30px;
            color: white;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }
        
        .history-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 2px 8px rgba(56, 189, 248, 0.3);
        }
        
        .main-content {
            max-width: 1400px;
            margin: 0 auto;
            padding: 32px 40px;
            position: relative;
            z-index: 10;
        }
        
        .search-card {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(16px);
            border-radius: 28px;
            padding: 32px 40px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.08);
            border: 1px solid rgba(56, 189, 248, 0.25);
            margin-bottom: 32px;
        }
        
        .card-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 28px;
            padding-bottom: 16px;
            border-bottom: 2px solid rgba(56, 189, 248, 0.3);
        }
        
        .card-header img {
            height: 36px;
            width: auto;
            object-fit: contain;
        }
        
        .card-header h2 {
            font-size: 22px;
            font-weight: 600;
            background: linear-gradient(135deg, #38bdf8, #8b5cf6);
            background-clip: text;
            -webkit-background-clip: text;
            color: transparent;
        }
        
        .form-row {
            display: flex;
            gap: 24px;
            flex-wrap: wrap;
            margin-bottom: 24px;
        }
        
        .form-group {
            flex: 1;
            min-width: 200px;
        }
        
        .form-group label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: #475569;
            margin-bottom: 8px;
        }
        
        .form-group input, .form-group select {
            width: 100%;
            padding: 12px 16px;
            background: white;
            border: 2px solid #e2e8f0;
            border-radius: 14px;
            font-size: 14px;
            outline: none;
            transition: all 0.2s;
        }
        
        .form-group input:focus, .form-group select:focus {
            border-color: #38bdf8;
            box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
        }
        
        .toggle-group {
            display: inline-flex;
            background: #f1f5f9;
            border-radius: 40px;
            padding: 4px;
        }
        
        .toggle-btn {
            padding: 10px 28px;
            border: none;
            background: transparent;
            border-radius: 36px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            color: #64748b;
            transition: all 0.2s;
        }
        
        .toggle-btn.active {
            background: linear-gradient(135deg, #38bdf8, #8b5cf6);
            color: white;
            box-shadow: 0 2px 8px rgba(56, 189, 248, 0.3);
        }
        
        .search-btn {
            display: block;
            margin: 20px auto 0;
            padding: 14px 56px;
            background: linear-gradient(135deg, #38bdf8, #8b5cf6);
            border: none;
            border-radius: 48px;
            font-size: 16px;
            font-weight: 600;
            color: white;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: 0 4px 14px rgba(56, 189, 248, 0.35);
        }
        
        .search-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(56, 189, 248, 0.4);
        }
        
        .search-btn:disabled {
            opacity: 0.7;
            transform: none;
            cursor: not-allowed;
        }
        
        .loading-container {
            text-align: center;
            padding: 80px 20px;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(16px);
            border-radius: 28px;
            margin-top: 24px;
            border: 1px solid rgba(56, 189, 248, 0.25);
        }
        
        .hourglass {
            width: 60px;
            height: 80px;
            margin: 0 auto 30px;
            position: relative;
            animation: rotate 2s linear infinite;
        }
        
        @keyframes rotate {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(180deg); }
        }
        
        .hourglass::before {
            content: '';
            position: absolute;
            top: 0;
            left: 50%;
            transform: translateX(-50%);
            border-width: 0 30px 40px 30px;
            border-style: solid;
            border-color: transparent transparent #38bdf8 transparent;
        }
        
        .hourglass::after {
            content: '';
            position: absolute;
            bottom: 0;
            left: 50%;
            transform: translateX(-50%);
            border-width: 40px 30px 0 30px;
            border-style: solid;
            border-color: #8b5cf6 transparent transparent transparent;
        }
        
        .hourglass-sand {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 8px;
            height: 8px;
            background: linear-gradient(135deg, #38bdf8, #8b5cf6);
            border-radius: 50%;
            animation: fall 1.5s ease-in-out infinite;
        }
        
        @keyframes fall {
            0% { top: 20%; opacity: 1; }
            50% { top: 50%; opacity: 0.6; }
            100% { top: 80%; opacity: 0; }
        }
        
        .loading-text {
            font-size: 18px;
            font-weight: 600;
            background: linear-gradient(135deg, #38bdf8, #8b5cf6);
            background-clip: text;
            -webkit-background-clip: text;
            color: transparent;
            margin-bottom: 12px;
        }
        
        .loading-subtext {
            color: #64748b;
            font-size: 13px;
            line-height: 1.6;
        }
        
        .progress-bar-container {
            width: 400px;
            max-width: 90%;
            height: 8px;
            background: #e2e8f0;
            border-radius: 10px;
            margin: 24px auto 16px;
            overflow: hidden;
        }
        
        .progress-bar {
            height: 100%;
            background: linear-gradient(90deg, #38bdf8, #8b5cf6);
            border-radius: 10px;
            animation: progress 2s ease-in-out infinite;
        }
        
        @keyframes progress {
            0% { width: 0%; }
            50% { width: 70%; }
            100% { width: 100%; }
        }
        
        .results-container {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(16px);
            border-radius: 28px;
            padding: 32px;
            border: 1px solid rgba(56, 189, 248, 0.25);
            margin-top: 24px;
        }
        
        .polymer-header {
            font-size: 24px;
            font-weight: bold;
            color: #1E88E5;
            margin: 20px 0;
            padding: 10px;
            border-left: 5px solid #1E88E5;
            background-color: #f0f8ff;
        }
        
        .polymer-subheader {
            font-size: 18px;
            font-weight: bold;
            color: #0d47a1;
            margin: 15px 0;
            padding: 5px;
            border-bottom: 2px solid #0d47a1;
        }
        
        .stats-row {
            display: flex;
            gap: 10px;
            margin: 15px 0;
        }
        
        .stat-card {
            flex: 1;
            background-color: white;
            padding: 10px;
            border-radius: 5px;
            text-align: center;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .stat-label {
            font-size: 12px;
            color: #666;
        }
        
        .stat-value {
            font-size: 24px;
            font-weight: bold;
            color: #1E88E5;
        }
        
        .chart-container {
            margin: 20px 0;
            text-align: center;
        }
        
        .chart-container img {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        
        .two-columns {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
        }
        
        .column {
            flex: 1;
            min-width: 300px;
        }
        
        .catalyst-table, .temp-table {
            width: 100%;
            border-collapse: collapse;
            background-color: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .catalyst-table th, .temp-table th {
            background-color: #4472C4;
            color: white;
            padding: 10px;
            text-align: left;
            font-size: 13px;
        }
        
        .catalyst-table td, .temp-table td {
            padding: 8px 10px;
            border-bottom: 1px solid #e5e7eb;
            font-size: 12px;
        }
        
        .catalyst-table tr:nth-child(even), .temp-table tr:nth-child(even) {
            background-color: #f9fafb;
        }
        
        .condition-summary {
            background-color: #f8fafc;
            padding: 15px;
            border-radius: 8px;
            font-size: 13px;
            line-height: 1.8;
        }
        
        .reaction-type-table {
            width: 80%;
            margin: 20px auto;
            border-collapse: collapse;
            background-color: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        
        .reaction-type-table th {
            background-color: #4472C4;
            color: white;
            padding: 12px 15px;
            text-align: left;
            font-size: 14px;
        }
        
        .reaction-type-table td {
            padding: 10px 15px;
            border-bottom: 1px solid #e5e7eb;
            font-size: 13px;
        }
        
        .reaction-type-table tr:hover {
            background-color: #f3f4f6;
        }
        
        .percentage-badge {
            background-color: #E7E6E6;
            padding: 4px 12px;
            border-radius: 12px;
            display: inline-block;
            font-size: 12px;
        }
        
        .synthesis-list {
            display: flex;
            flex-direction: column;
            gap: 12px;
            max-height: 400px;
            overflow-y: auto;
        }
        
        .synthesis-item {
            background: #f8fafc;
            border-left: 3px solid #38bdf8;
            padding: 14px 16px;
            border-radius: 12px;
        }
        
        .synthesis-method {
            font-weight: 600;
            color: #1e293b;
            margin-bottom: 8px;
        }
        
        .synthesis-details {
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            font-size: 12px;
            color: #64748b;
        }
        
        .warning-banner {
            background-color: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 12px 16px;
            margin-bottom: 20px;
            border-radius: 8px;
            color: #856404;
            font-size: 13px;
        }
        
        /* DeepSeek风格历史记录模态框 */
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.5);
            backdrop-filter: blur(4px);
        }
        
        .modal-content {
            background: white;
            margin: 10% auto;
            padding: 0;
            width: 90%;
            max-width: 800px;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            overflow: hidden;
            animation: slideIn 0.2s ease-out;
        }
        
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(-30px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        .modal-header {
            background: white;
            color: #1e293b;
            padding: 20px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #e2e8f0;
        }
        
        .modal-header h3 {
            margin: 0;
            font-size: 18px;
            font-weight: 600;
        }
        
        .close-modal {
            font-size: 28px;
            cursor: pointer;
            color: #94a3b8;
            transition: all 0.2s;
            line-height: 1;
        }
        
        .close-modal:hover {
            color: #1e293b;
        }
        
        .modal-body {
            padding: 0;
            max-height: 500px;
            overflow-y: auto;
        }
        
        .history-list {
            list-style: none;
            margin: 0;
            padding: 0;
        }
        
        .history-item {
            padding: 16px 20px;
            border-bottom: 1px solid #f1f5f9;
            cursor: pointer;
            transition: background 0.15s;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .history-item:hover {
            background: #f8fafc;
        }
        
        .history-info {
            flex: 1;
        }
        
        .history-material {
            font-weight: 600;
            color: #1e293b;
            font-size: 15px;
            margin-bottom: 6px;
        }
        
        .history-meta {
            display: flex;
            gap: 16px;
            font-size: 12px;
            color: #94a3b8;
            flex-wrap: wrap;
        }
        
        .history-badge {
            background: #e2e8f0;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            color: #475569;
        }
        
        .mode-badge {
            background: #e0f2fe;
            color: #0284c7;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            margin-left: 8px;
        }
        
        .mode-badge.property {
            background: #fce7f3;
            color: #db2777;
        }
        
        .delete-history-btn {
            background: none;
            border: none;
            color: #ef4444;
            cursor: pointer;
            padding: 8px;
            border-radius: 6px;
            font-size: 14px;
            transition: all 0.2s;
        }
        
        .delete-history-btn:hover {
            background: #fef2f2;
        }
        
        .empty-history {
            text-align: center;
            padding: 60px 20px;
            color: #94a3b8;
            font-size: 14px;
        }
        
        .clear-history-btn {
            background: none;
            color: #ef4444;
            border: none;
            padding: 6px 12px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            transition: all 0.2s;
        }
        
        .clear-history-btn:hover {
            background: #fef2f2;
        }
        
        footer {
            text-align: center;
            padding: 24px;
            font-size: 12px;
            color: #94a3b8;
            background: rgba(255, 255, 255, 0.8);
            backdrop-filter: blur(8px);
            border-top: 1px solid rgba(56, 189, 248, 0.2);
        }
        
        @media (max-width: 768px) {
            nav { padding: 12px 20px; }
            .main-content { padding: 20px; }
            .search-card { padding: 24px; }
            .stats-row { flex-direction: column; }
            .two-columns { flex-direction: column; }
            .reaction-type-table { width: 100%; }
            .modal-content { width: 95%; margin: 20% auto; }
            .history-item { flex-direction: column; align-items: flex-start; gap: 10px; }
            .history-meta { flex-direction: column; gap: 6px; }
        }
    </style>
</head>
<body>

<div class="particles" id="particles"></div>

<nav>
    <div class="logo-area">
        <img src="/static/logo.png" alt="PolyMind Logo" onerror="this.style.display='none'">
        <span>PolyMind</span>
    </div>
    <div class="history-toggle">
        <button class="history-btn" onclick="showHistory()">History</button>
    </div>
</nav>

<div class="main-content">
    <div class="search-card">
        <div class="card-header">
            <img src="/static/logo.png" alt="Logo" onerror="this.style.display='none'">
            <h2>Material Search</h2>
        </div>

        <div class="form-row">
            <div class="form-group" style="flex: 2;">
                <label>Material Name</label>
                <input type="text" id="materialInput" placeholder="e.g. Polyethylene, Polyimide, Nylon-6,6">
            </div>
        </div>

        <div class="form-row">
            <div class="form-group">
                <label>Target Information</label>
                <div class="toggle-group">
                    <button class="toggle-btn active" onclick="setMode('synthesis', this)">Synthesis</button>
                    <button class="toggle-btn" onclick="setMode('property', this)">Properties</button>
                </div>
            </div>
            <div class="form-group">
                <label>Number of Literature Papers</label>
                <input type="number" id="maxPapersInput" placeholder="e.g. 500" min="1" max="2000" step="1">
            </div>
        </div>

        <div class="form-row">
            <div class="form-group">
                <label>API Key</label>
                <input type="password" id="apiKeyInput" placeholder="Please enter your API Key">
            </div>
            <div class="form-group">
                <label>Base URL</label>
                <input type="text" id="baseUrlInput" placeholder="API Base URL (e.g. https://api.openai.com/v1)">
            </div>
            <div class="form-group">
                <label>Model Name</label>
                <input type="text" id="modelInput" placeholder="Model Name (e.g. gpt-4, deepseek-chat)">
            </div>
        </div>

        <button class="search-btn" id="searchBtn" onclick="startSearch()">SEARCH</button>
    </div>

    <div id="resultsArea"></div>
</div>

<footer>PolyMind © 2026 · Powered by AI Knowledge Retrieval System</footer>

<!-- DeepSeek风格历史记录模态框 -->
<div id="historyModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            <h3> Search History</h3>
            <div>
                <button id="clearAllHistoryBtn" class="clear-history-btn" style="margin-right: 12px;">Clear all</button>
                <span class="close-modal" onclick="closeHistory()">&times;</span>
            </div>
        </div>
        <div class="modal-body" id="historyList">
            <div class="empty-history">Loading...</div>
        </div>
    </div>
</div>

<script>
    let currentMode = 'synthesis';
    
    function setMode(mode, btn) {
        currentMode = mode;
        document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        console.log('Mode changed to:', mode);
    }
    
    async function startSearch() {
        const material = document.getElementById('materialInput').value.trim();
        if (!material) {
            alert('Please enter a material name');
            return;
        }
        
        const apiKey = document.getElementById('apiKeyInput')?.value || '';
        const baseUrl = document.getElementById('baseUrlInput')?.value || '';
        const model = document.getElementById('modelInput')?.value || '';
        let maxPapers = document.getElementById('maxPapersInput')?.value || '';
        
        if (!apiKey || !baseUrl || !model) {
            alert('Please enter API Key, Base URL, and Model');
            return;
        }
        
        // 验证maxPapers，如果为空则使用默认值500
        let papersNum = 500;
        if (maxPapers && maxPapers.trim() !== '') {
            papersNum = parseInt(maxPapers);
            if (isNaN(papersNum) || papersNum < 1) {
                papersNum = 50;
            }
            if (papersNum > 2000) {
                if (!confirm('Analyzing more than 2000 papers may take a long time. Continue?')) {
                    return;
                }
            }
        }
        
        const searchBtn = document.getElementById('searchBtn');
        const resultsArea = document.getElementById('resultsArea');
        
        searchBtn.disabled = true;
        searchBtn.textContent = 'SEARCHING...';
        
        resultsArea.innerHTML = `
            <div class="loading-container">
                <div class="hourglass"><div class="hourglass-sand"></div></div>
                <div class="loading-text">Currently retrieving literature and analyzing synthesis conditions...</div>
                <div class="loading-subtext">
                    Conducting searches in databases including PubMed, Semantic Scholar, OpenAlex, and arXiv<br>
                    Employing AI to extract synthetic methods and reaction conditions<br>
                </div>
                <div class="progress-bar-container"><div class="progress-bar"></div></div>
                <div class="progress-text">Estimated to require 5 to 10 minutes.</div>
            </div>
        `;
        
        try {
            const response = await fetch('/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    material,
                    mode: currentMode,
                    api_key: apiKey,
                    base_url: baseUrl,
                    model: model,
                    max_papers: papersNum
                })
            });
            
            if (!response.ok) {
                const text = await response.text();
                throw new Error(`Server error ${response.status}: ${text.substring(0, 200)}`);
            }
            
            const data = await response.json();
            
            if (data.error) {
                resultsArea.innerHTML = `
                    <div class="results-container" style="text-align:center; color:#dc2626;">
                        <div style="font-size:48px; margin-bottom:16px;"></div>
                        <div>Error: ${data.error}</div>
                    </div>`;
                return;
            }
            
            window.currentDataframe = data.dataframe || [];
            renderResults(data, material);
        } catch (error) {
            resultsArea.innerHTML = `
                <div class="results-container" style="text-align:center; color:#dc2626;">
                    <div style="font-size:48px; margin-bottom:16px;"></div>
                    <div>Request failed: ${error.message}</div>
                </div>`;
        } finally {
            searchBtn.disabled = false;
            searchBtn.textContent = 'SEARCH';
        }
    }
    
    function renderResults(data, material) {
        console.log('RenderResults data:', data);
        const resultsArea = document.getElementById('resultsArea');
        const syntheses = data.syntheses || [];
        const stats = data.stats || {};
        const exampleUsed = data.exampleUsed || false;
        
        // Store dataframe for CSV export
        window.currentDataframe = data.dataframe || [];
        
        let warningHtml = '';
        if (exampleUsed) {
            warningHtml = `
                <div class="warning-banner">
                     No relevant literature found. Displaying sample data for reference.
                </div>
            `;
        }
        
        // 合成方法列表
        let synthesesHtml = '';
        if (syntheses.length > 0) {
            syntheses.forEach((syn, idx) => {
                synthesesHtml += `
                    <div class="synthesis-item">
                        <div class="synthesis-method">Method ${idx + 1}: ${syn.method || 'Polymerization'}</div>
                        <div class="synthesis-details">
                            <span>🌡️ Temperature: ${syn.temperature || 'Not Provided'}</span>
                            <span>⚡ Catalyst: ${syn.catalyst || 'Not Provided'}</span>
                            <span>⏱️ Time: ${syn.time || 'Not Provided'}</span>
                            <span>🧪 Solvent: ${syn.solvent || 'Not Provided'}</span>
                            <span>🧬 Reactants: ${syn.reactants || 'Not Provided'}</span>
                        </div>
                    </div>
                `;
            });
        } else {
            synthesesHtml = '<div style="text-align: center; padding: 40px; color: #94a3b8;">No synthesis data available</div>';
        }
        
        // 温度标签表（只在有数据时显示）
        let tempLabelsHtml = '';
        if (data.tempLabels && data.tempLabels.length > 0) {
            tempLabelsHtml = `
                <div class="polymer-subheader"> Temperature Details</div>
                <table class="temp-table">
                    <thead>
                        <tr><th>Temperature Expression</th><th>Count</th></tr>
                    </thead>
                    <tbody>
                        ${data.tempLabels.map(item => `<tr><td>${item.label}</td><td>${item.count}</td>`).join('')}
                    </tbody>
                </table>
            `;
        }
        
        // 催化剂表（只在有数据时显示）
        let catalystHtml = '';
        if (data.catalystTable && data.catalystTable.length > 0) {
            catalystHtml = `
                <div class="polymer-subheader"> Common Catalysts</div>
                <table class="catalyst-table">
                    <thead>
                        <tr><th>Catalyst</th><th>Count</th></tr>
                    </thead>
                    <tbody>
                        ${data.catalystTable.map(item => `<tr><td>${item.name}</td><td>${item.count}</td>`).join('')}
                    </tbody>
                </table>
            `;
        }
        
        // 条件汇总
        let conditionHtml = '';
        if (data.conditionSummary && data.conditionSummary.length > 0) {
            conditionHtml = `
                <div class="polymer-subheader"> Conditions Summary</div>
                <div class="condition-summary">
                    ${data.conditionSummary.map(item => `<div>${item}</div>`).join('')}
                </div>
            `;
        }
        
        // 反应类型表（只在有数据时显示）
        let reactionTypeTableHtml = '';
        if (data.reactionTypeTable && data.reactionTypeTable.length > 0) {
            reactionTypeTableHtml = `
                <div class="polymer-subheader">Reaction Type Distribution</div>
                <table class="reaction-type-table">
                    <thead>
                        <tr><th>Reaction Type</th><th>Count</th><th>Percentage</th></tr>
                    </thead>
                    <tbody>
                        ${data.reactionTypeTable.map(item => `
                            <tr>
                                <td>${item.type}</td>
                                <td>${item.count}</td>
                                <td><span class="percentage-badge">${item.percentage}%</span></td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }
        
        let content = `
            <div class="results-container">
                ${warningHtml}
                <div class="polymer-header"> Polymer Knowledge Retrieval Results: ${material}</div>
                
                <div class="stats-row">
                    <div class="stat-card">
                        <div class="stat-label">Total Syntheses</div>
                        <div class="stat-value">${stats.withSynthesis || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">With Temperature</div>
                        <div class="stat-value">${stats.hasTemperature || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">With Catalyst</div>
                        <div class="stat-value">${stats.hasCatalyst || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">With Solvent</div>
                        <div class="stat-value">${stats.hasReactants || 0}</div>
                    </div>
                </div>
        `;
        
        // 温度分布饼图（只在有数据时显示）
        if (data.tempPlot) {
            content += `
                <div class="chart-container">
                    <h3> Temperature Distribution</h3>
                    <img src="data:image/png;base64,${data.tempPlot}" alt="Temperature Distribution">
                </div>
            `;
        }
        
        // 温度详情表
        content += tempLabelsHtml;
        
        // 溶剂饼图和催化剂表（只在有数据时显示）
        if (data.solventPie || catalystHtml) {
            content += `<div class="two-columns">`;
            if (data.solventPie) {
                content += `
                    <div class="column">
                        <div class="chart-container">
                            <h3> Solvent Distribution</h3>
                            <img src="data:image/png;base64,${data.solventPie}" alt="Solvent Distribution">
                        </div>
                    </div>
                `;
            }
            if (catalystHtml) {
                content += `<div class="column">${catalystHtml}</div>`;
            }
            content += `</div>`;
        }
        
        // 反应类型图（只在有数据时显示）
        if (data.reactionPlot) {
            content += `
                <div class="chart-container">
                    <h3> Reaction Type Distribution</h3>
                    <img src="data:image/png;base64,${data.reactionPlot}" alt="Reaction Type Distribution">
                </div>
            `;
        }
        
        // 反应类型表
        content += reactionTypeTableHtml;
        
        // 条件汇总
        content += conditionHtml;
        
        // 合成方法列表
        content += `
            <div class="polymer-subheader">Extracted Synthesis Methods (${syntheses.length})</div>
            <div class="synthesis-list">${synthesesHtml}</div>
            
            <div style="display: flex; justify-content: center; gap: 16px; margin-top: 24px;">
                <button onclick="exportToCSV()" style="padding: 10px 24px; background: #28a745; border: none; border-radius: 40px; color: white; cursor: pointer;"> Export CSV</button>
                <button onclick="location.reload()" style="padding: 10px 24px; background: linear-gradient(135deg, #38bdf8, #8b5cf6); border: none; border-radius: 40px; color: white; cursor: pointer;">New Search</button>
            </div>
        `;
        
        content += `</div>`;
        resultsArea.innerHTML = content;
    }
    
    // CSV 导出函数
    async function exportToCSV() {
        const dataframe = window.currentDataframe;
        if (!dataframe || dataframe.length === 0) {
            alert('No data to export');
            return;
        }
        
        try {
            const response = await fetch('/api/export_csv', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    data: dataframe,
                    filename: `synthesis_results_${new Date().toISOString().slice(0,19).replace(/:/g, '-')}.csv`
                })
            });
            
            const result = await response.json();
            if (result.success) {
                // Create download link
                const blob = new Blob([result.csv_content], { type: 'text/csv;charset=utf-8;' });
                const link = document.createElement('a');
                const url = URL.createObjectURL(blob);
                link.href = url;
                link.setAttribute('download', result.filename);
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                URL.revokeObjectURL(url);
            } else {
                alert('Export failed: ' + result.error);
            }
        } catch (error) {
            alert('Export error: ' + error.message);
        }
    }
    
    // 显示历史记录
    async function showHistory() {
        const modal = document.getElementById('historyModal');
        const historyList = document.getElementById('historyList');
        modal.style.display = 'block';
        historyList.innerHTML = '<div class="empty-history">Loading...</div>';
        
        try {
            const response = await fetch('/api/history');
            const data = await response.json();
            const history = data.history || [];
            
            if (history.length === 0) {
                historyList.innerHTML = '<div class="empty-history">No search history yet</div>';
                return;
            }
            
            historyList.innerHTML = `
                <ul class="history-list">
                    ${history.map((item, idx) => `
                        <li class="history-item" onclick="restoreSearchResult(${idx})">
                            <div class="history-info">
                                <div class="history-material">
                                    ${item.material}
                                    <span class="mode-badge ${item.mode === 'property' ? 'property' : ''}">
                                        ${item.mode === 'synthesis' ? 'Synthesis' : 'Properties'}
                                    </span>
                                </div>
                                <div class="history-meta">
                                    <span>📅 ${item.timestamp}</span>
                                    <span>📄 ${item.papers_found} papers</span>
                                    <span>⚗️ ${item.reactions_extracted} reactions</span>
                                    <span>🔍 max ${item.max_papers || 500}</span>
                                </div>
                            </div>
                            <button class="delete-history-btn" onclick="event.stopPropagation(); deleteHistoryItem(${idx})">🗑️</button>
                        </li>
                    `).join('')}
                </ul>
            `;
        } catch (error) {
            historyList.innerHTML = '<div class="empty-history">Failed to load history</div>';
        }
    }
    
    // 恢复搜索结果
    async function restoreSearchResult(index) {
        closeHistory();
        
        const resultsArea = document.getElementById('resultsArea');
        resultsArea.innerHTML = `
            <div class="loading-container">
                <div class="hourglass"><div class="hourglass-sand"></div></div>
                <div class="loading-text">Restoring saved results...</div>
                <div class="loading-subtext">Loading previously saved synthesis data</div>
                <div class="progress-bar-container"><div class="progress-bar"></div></div>
            </div>
        `;
        
        try {
            const response = await fetch('/api/history');
            const data = await response.json();
            const history = data.history || [];
            
            if (history[index] && history[index].result_data) {
                const savedData = history[index].result_data;
                const material = history[index].material;
                
                const restoredData = {
                    stats: savedData.stats || {},
                    syntheses: savedData.syntheses || [],
                    material: material,
                    mode: history[index].mode,
                    totalPapers: history[index].papers_found,
                    exampleUsed: false,
                    tempPlot: savedData.tempPlot,
                    solventPie: savedData.solventPie,
                    reactionPlot: savedData.reactionPlot,
                    catalystTable: savedData.catalystTable || [],
                    tempLabels: savedData.tempLabels || [],
                    conditionSummary: savedData.conditionSummary || [],
                    reactionTypeTable: savedData.reactionTypeTable || [],
                    dataframe: savedData.dataframe || []
                };
                
                window.currentDataframe = restoredData.dataframe;
                renderResults(restoredData, material);
            } else {
                resultsArea.innerHTML = `
                    <div class="results-container" style="text-align:center; color:#dc2626;">
                        <div>Incomplete history data, please search again</div>
                    </div>`;
            }
        } catch (error) {
            resultsArea.innerHTML = `
                <div class="results-container" style="text-align:center; color:#dc2626;">
                    <div>Restore failed: ${error.message}</div>
                </div>`;
        }
    }
    
    // 删除单条历史记录
    async function deleteHistoryItem(index) {
        if (!confirm('Delete this search record?')) return;
        
        try {
            const response = await fetch(`/api/history/${index}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            
            if (result.success) {
                showHistory();
            } else {
                alert('Delete failed: ' + result.error);
            }
        } catch (error) {
            alert('Delete failed: ' + error.message);
        }
    }
    
    // 清空所有历史记录
    async function clearAllHistory() {
        if (!confirm('Clear all search history? This cannot be undone.')) return;
        
        try {
            await fetch('/api/history/clear', { method: 'POST' });
            showHistory();
        } catch (error) {
            alert('Clear failed: ' + error.message);
        }
    }
    
    // 关闭历史记录
    function closeHistory() {
        document.getElementById('historyModal').style.display = 'none';
    }
    
    // 重放搜索
    function replaySearch(material, mode) {
        closeHistory();
        document.getElementById('materialInput').value = material;
        
        const modeButtons = document.querySelectorAll('.toggle-btn');
        if (mode === 'synthesis') {
            if (modeButtons[0]) modeButtons[0].click();
        } else {
            if (modeButtons[1]) modeButtons[1].click();
        }
        
        window.scrollTo({ top: 0, behavior: 'smooth' });
        setTimeout(() => startSearch(), 500);
    }
    
    // 点击模态框外部关闭
    window.onclick = function(event) {
        const modal = document.getElementById('historyModal');
        if (event.target === modal) {
            closeHistory();
        }
    }
    
    // 粒子效果
    function createParticles() {
        const container = document.getElementById('particles');
        if (!container) return;
        for (let i = 0; i < 50; i++) {
            const particle = document.createElement('div');
            particle.className = 'particle';
            particle.style.width = Math.random() * 6 + 2 + 'px';
            particle.style.height = particle.style.width;
            particle.style.left = Math.random() * 100 + '%';
            particle.style.top = Math.random() * 100 + '%';
            particle.style.animationDelay = Math.random() * 15 + 's';
            particle.style.animationDuration = Math.random() * 10 + 10 + 's';
            container.appendChild(particle);
        }
    }
    
    // 回车键支持
    document.getElementById('materialInput')?.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') startSearch();
    });
    
    // 清空全部历史按钮事件
    document.getElementById('clearAllHistoryBtn')?.addEventListener('click', clearAllHistory);
    
    // 页面加载完成
    window.addEventListener('load', function() {
        console.log('PolyMind ready!');
        createParticles();
    });
</script>
</body>
</html>
'''

if __name__ == '__main__':
    app.run(
        debug=os.getenv('FLASK_DEBUG', '').lower() in {'1', 'true', 'yes', 'on'},
        host=os.getenv('HOST', '127.0.0.1'),
        port=int(os.getenv('PORT', '5002')),
    )
