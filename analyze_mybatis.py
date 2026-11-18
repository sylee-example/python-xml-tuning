"""MyBatis 매퍼 XML 쿼리를 Oracle 스키마(DDL)와 대조해 개선 리포트를 docs/ 에 생성한다.

사용법:
  python analyze_mybatis.py [매퍼_폴더] [--env .env] [--out-dir docs]

.env 항목:
  DB_USER, DB_PASSWORD, DB_DSN   (필수) 예: DB_DSN=dbhost:1521/ORCLPDB1
  DB_SCHEMA                      (선택) 조회할 스키마(OWNER), 콤마 구분. 기본값은 DB_USER
  MAPPER_DIR                     (선택) 매퍼 XML 폴더. 실행 인자가 우선
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import oracledb
import sqlglot
from dotenv import dotenv_values
from sqlglot import exp
from sqlglot.errors import SqlglotError

STATEMENT_TAGS = ('select', 'insert', 'update', 'delete')
QUERY_NODES = (exp.Select, exp.Update, exp.Delete, exp.Merge, exp.Insert)
SKIP_TABLES = {'DUAL'}
# 컬럼처럼 파싱되지만 실제 컬럼이 아닌 Oracle 의사 컬럼/함수
PSEUDO_COLUMNS = {
  'ROWNUM', 'ROWID', 'LEVEL', 'SYSDATE', 'SYSTIMESTAMP', 'CURRENT_DATE',
  'CURRENT_TIMESTAMP', 'USER', 'NULL', 'CONNECT_BY_ISLEAF', 'CONNECT_BY_ISCYCLE',
}
SEVERITY_ORDER = ('높음', '중간', '낮음')

FUNC_HINTS = {
  'TRUNC': '범위 조건으로 변경: 컬럼 >= :d AND 컬럼 < :d + 1',
  'TO_CHAR': '컬럼에 TO_CHAR를 쓰지 말고 비교 값을 변환하거나 범위 조건(>=, <)으로 변경',
  'NVL': '(컬럼 = :v OR 컬럼 IS NULL) 형태로 변경',
  'COALESCE': '(컬럼 = :v OR 컬럼 IS NULL) 형태로 변경',
  'SUBSTR': "선두 일치라면 컬럼 LIKE :v || '%' 로 변경",
  'UPPER': '함수 기반 인덱스(FBI) 생성 또는 대소문자 정규화 컬럼 사용',
  'LOWER': '함수 기반 인덱스(FBI) 생성 또는 대소문자 정규화 컬럼 사용',
}
DEFAULT_FUNC_HINT = '함수 기반 인덱스(FBI) 생성 또는 연산을 비교 값 쪽으로 이동'


# ---------------------------------------------------------------- 데이터 모델

@dataclass
class TableInfo:
  columns: set[str] = field(default_factory=set)
  indexes: dict[str, list[str]] = field(default_factory=dict)  # 인덱스명 -> 컬럼(순서 유지)
  is_view: bool = False
  opaque: bool = False  # 컬럼 정보를 알 수 없는 대상(다른 스키마로 향하는 시노님 등)


@dataclass
class Schema:
  tables: dict[str, TableInfo] = field(default_factory=dict)
  synonyms: dict[str, str] = field(default_factory=dict)

  def resolve(self, name: str) -> TableInfo | None:
    """테이블/뷰/시노님 이름으로 TableInfo 조회. 없으면 None."""
    name = name.upper()
    if name in self.tables:
      return self.tables[name]
    if name in self.synonyms:
      return self.tables.get(self.synonyms[name], TableInfo(opaque=True))
    return None


@dataclass
class Finding:
  severity: str
  title: str
  detail: str


@dataclass
class SchemaIssue:
  kind: str  # 테이블 / 컬럼 / 별칭
  name: str
  hint: str


@dataclass
class Query:
  file: str
  namespace: str
  id: str
  kind: str
  sql: str
  dollars: list[str]
  findings: list[Finding] = field(default_factory=list)
  issues: list[SchemaIssue] = field(default_factory=list)
  pretty: str = ''
  parse_error: str = ''

  @property
  def full_id(self) -> str:
    return f'{self.namespace}.{self.id}' if self.namespace else self.id


# ---------------------------------------------------------------- DB(DDL) 읽기

def _in_clause(values: list[str]) -> tuple[str, dict[str, str]]:
  binds = {f'o{i}': v for i, v in enumerate(values)}
  return ','.join(f':{k}' for k in binds), binds


def load_schema(conn: oracledb.Connection, owners: list[str]) -> Schema:
  """딕셔너리 뷰(ALL_*)에서 테이블/컬럼/인덱스/뷰/시노님 정보를 읽는다. 읽기 전용."""
  schema = Schema()
  marks, binds = _in_clause(owners)
  with conn.cursor() as cur:
    cur.execute(f'SELECT table_name, column_name FROM all_tab_columns WHERE owner IN ({marks})', binds)
    for table, column in cur:
      schema.tables.setdefault(table.upper(), TableInfo()).columns.add(column.upper())

    cur.execute(f'SELECT view_name FROM all_views WHERE owner IN ({marks})', binds)
    for (view,) in cur:
      if view.upper() in schema.tables:
        schema.tables[view.upper()].is_view = True

    cur.execute(
      'SELECT table_name, index_name, column_name FROM all_ind_columns '
      f'WHERE table_owner IN ({marks}) ORDER BY table_name, index_name, column_position',
      binds,
    )
    for table, index, column in cur:
      info = schema.tables.get(table.upper())
      if info is not None:
        info.indexes.setdefault(index.upper(), []).append(column.upper())

    cur.execute(
      f"SELECT synonym_name, table_name FROM all_synonyms WHERE owner IN ({marks}) OR owner = 'PUBLIC'",
      binds,
    )
    for synonym, target in cur:
      schema.synonyms.setdefault(synonym.upper(), target.upper())
  return schema


# ---------------------------------------------------------------- MyBatis XML -> SQL

def read_xml(path: Path) -> ET.Element:
  """expat 은 EUC-KR 같은 멀티바이트 인코딩 선언을 못 읽으므로 직접 디코딩 후 선언을 제거한다."""
  raw = path.read_bytes()
  match = re.match(rb'\s*(?:\xef\xbb\xbf)?<\?xml[^>]*encoding=["\']([\w.-]+)["\']', raw)
  encoding = match.group(1).decode('ascii') if match else 'utf-8'
  try:
    text = raw.decode(encoding)
  except (UnicodeDecodeError, LookupError):
    text = raw.decode('cp949', errors='replace')
  text = re.sub(r'^\s*<\?xml[^>]*\?>', '', text.lstrip('\ufeff'))
  return ET.fromstring(text)


def _trim(text: str, prefix: str = '', suffix: str = '',
          prefix_overrides: tuple[str, ...] = (), suffix_overrides: tuple[str, ...] = ()) -> str:
  body = text.strip()
  if not body:
    return ''
  for token in prefix_overrides:
    token = token.strip()
    boundary = r'\b' if token[-1:].isalnum() else ''
    body = re.sub(rf'^{re.escape(token)}{boundary}\s*', '', body, count=1, flags=re.I)
  for token in suffix_overrides:
    body = re.sub(rf'\s*{re.escape(token.strip())}\s*$', '', body, count=1, flags=re.I)
  return f' {prefix} {body} {suffix} '


def _split_overrides(value: str | None) -> tuple[str, ...]:
  return tuple(v for v in (value or '').split('|') if v.strip())


def flatten(el: ET.Element, fragments: dict[str, ET.Element], props: dict[str, str], depth: int = 0) -> str:
  parts = [el.text or '']
  for child in el:
    parts.append(_render(child, fragments, props, depth))
    parts.append(child.tail or '')
  return ''.join(parts)


def _render(el: ET.Element, fragments: dict[str, ET.Element], props: dict[str, str], depth: int) -> str:
  tag = el.tag
  if tag in ('selectKey', 'bind', 'property'):
    return ' '
  if tag == 'include':
    fragment = fragments.get(el.get('refid', ''))
    if fragment is None or depth > 10:  # 순환 include 방지
      return ' '
    local = {**props, **{p.get('name', ''): p.get('value', '') for p in el.findall('property')}}
    inner = flatten(fragment, fragments, local, depth + 1)
    return re.sub(r'\$\{(\w+)\}', lambda m: local.get(m.group(1), m.group(0)), inner)
  if tag == 'choose':  # 분기가 여러 개면 SQL 이 깨지므로 첫 when 만 사용
    first = next((c for c in el if c.tag == 'when'), None)
    return flatten(first, fragments, props, depth) if first is not None else ' '
  inner = flatten(el, fragments, props, depth)
  if tag == 'foreach':
    return f"{el.get('open', '')}{inner}{el.get('close', '')}"
  if tag == 'where':
    return _trim(inner, 'WHERE', prefix_overrides=('AND', 'OR'))
  if tag == 'set':
    return _trim(inner, 'SET', prefix_overrides=(',',), suffix_overrides=(',',))
  if tag == 'trim':
    return _trim(
      inner, el.get('prefix', ''), el.get('suffix', ''),
      _split_overrides(el.get('prefixOverrides')), _split_overrides(el.get('suffixOverrides')),
    )
  return inner  # if / when / otherwise 등은 항상 참으로 가정하고 모두 포함


def normalize(text: str) -> tuple[str, list[str]]:
  """#{} -> :b, ${} -> __RAW__ 로 치환하고 주석/공백을 정리한다. ${} 목록도 함께 반환."""
  dollars = re.findall(r'\$\{[^}]*\}', text)
  text = re.sub(r"('(?:[^']|'')*')|--[^\n]*", lambda m: m.group(1) or '', text)  # 문자열은 보존, -- 주석 제거
  text = re.sub(r'#\{[^}]*\}', ':b', text)
  text = re.sub(r'\$\{[^}]*\}', '__RAW__', text)
  text = re.sub(r'\s+', ' ', text).strip().rstrip(';').strip()
  return text, dollars


def collect_queries(mapper_dir: Path) -> tuple[list[Query], list[tuple[str, str]]]:
  roots: list[tuple[str, ET.Element]] = []
  xml_errors: list[tuple[str, str]] = []
  for path in sorted(mapper_dir.rglob('*.xml')):
    rel = path.relative_to(mapper_dir).as_posix()
    try:
      root = read_xml(path)
    except (ET.ParseError, OSError) as e:
      xml_errors.append((rel, str(e)))
      continue
    if root.tag == 'mapper':  # pom.xml, mybatis-config.xml 등은 제외
      roots.append((rel, root))

  fragments: dict[str, ET.Element] = {}
  for _, root in roots:
    namespace = root.get('namespace', '')
    for el in root.findall('sql'):
      fragments[el.get('id', '')] = el
      fragments[f"{namespace}.{el.get('id', '')}"] = el

  queries: list[Query] = []
  for rel, root in roots:
    for el in root:
      if el.tag in STATEMENT_TAGS:
        sql, dollars = normalize(flatten(el, fragments, {}))
        queries.append(Query(rel, root.get('namespace', ''), el.get('id', ''), el.tag, sql, dollars))
  return queries, xml_errors


# ---------------------------------------------------------------- SQL 분석

def owner_of(node: exp.Expression) -> exp.Expression | None:
  """node 를 감싸는 가장 가까운 쿼리 노드(SELECT/UPDATE/...)."""
  parent = node.parent
  while parent is not None and not isinstance(parent, QUERY_NODES):
    parent = parent.parent
  return parent


def _unwrap(node: exp.Expression) -> exp.Expression:
  while isinstance(node, exp.Paren):
    node = node.this
  return node


def _leading_wildcard(pattern: exp.Expression) -> bool:
  """LIKE 패턴이 '%' 로 시작하면 True ('%' || :b, CONCAT('%', :b) 포함)."""
  pattern = _unwrap(pattern)
  while isinstance(pattern, exp.DPipe):
    pattern = _unwrap(pattern.this)
  if isinstance(pattern, exp.Concat) and pattern.expressions:
    pattern = pattern.expressions[0]
  return isinstance(pattern, exp.Literal) and pattern.is_string and pattern.name.startswith('%')


class Checker:
  def __init__(self, tree: exp.Expression, schema: Schema, query: Query) -> None:
    self.tree, self.schema, self.q = tree, schema, query
    self.ctes = {c.alias.upper() for c in tree.find_all(exp.CTE)}
    self.select_aliases = {a.alias.upper() for a in tree.find_all(exp.Alias) if a.alias}
    self._sources: dict[int, dict[str, exp.Table | None]] = {}

  def run(self) -> None:
    self.check_tables()
    self.check_columns()
    self.check_insert_columns()
    self.check_patterns()
    self.check_predicates()

  # --- 결과 기록
  def issue(self, kind: str, name: str, hint: str) -> None:
    if not any(i.kind == kind and i.name == name for i in self.q.issues):
      self.q.issues.append(SchemaIssue(kind, name, hint))

  def add(self, severity: str, title: str, detail: str) -> None:
    if not any(f.title == title and f.detail == detail for f in self.q.findings):
      self.q.findings.append(Finding(severity, title, detail))

  @staticmethod
  def similar(name: str, candidates) -> str:
    close = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    return f'유사: {", ".join(close)}' if close else ''

  # --- 스코프 해석: 쿼리 노드가 직접 참조하는 테이블/인라인뷰/CTE (None 은 스키마로 검증 불가한 파생 소스)
  def sources_of(self, node: exp.Expression) -> dict[str, exp.Table | None]:
    cached = self._sources.get(id(node))
    if cached is not None:
      return cached
    out: dict[str, exp.Table | None] = {}
    for table in node.find_all(exp.Table):
      if owner_of(table) is not node or not table.name:
        continue
      derived = table.name.upper() in self.ctes and not table.db
      out[(table.alias or table.name).upper()] = None if derived else table
    for sub in node.find_all(exp.Subquery):
      if sub.alias and owner_of(sub) is node:
        out[sub.alias.upper()] = None
    self._sources[id(node)] = out
    return out

  def info_of(self, table: exp.Table | None) -> TableInfo | None:
    return None if table is None else self.schema.resolve(table.name)

  def locate(self, col: exp.Column):
    """컬럼이 속한 테이블 해석. ('found'|'missing'|'unknown'|'badalias', 테이블들, TableInfo들)"""
    qualifier, name = col.table.upper(), col.name.upper()
    node = owner_of(col)
    first_level: tuple[list, list] | None = None
    while node is not None:
      sources = self.sources_of(node)
      if qualifier:
        if qualifier in sources:
          table = sources[qualifier]
          info = self.info_of(table)
          if info is None or info.opaque:
            return 'unknown', [], []
          return ('found' if name in info.columns else 'missing'), [table], [info]
      elif sources:
        tables = list(sources.values())
        infos = [self.info_of(t) for t in tables]
        if any(i is None or i.opaque for i in infos):  # 파생/미확인 소스가 있으면 판단 불가
          return 'unknown', [], []
        for table, info in zip(tables, infos):
          if name in info.columns:
            return 'found', [table], [info]
        first_level = first_level or (tables, infos)  # 상관 서브쿼리일 수 있어 바깥 스코프도 확인
      node = owner_of(node)
    if qualifier:
      return 'badalias', [], []
    return ('missing', *first_level) if first_level else ('unknown', [], [])

  # --- 스키마 대조
  def check_tables(self) -> None:
    candidates = list(self.schema.tables) + list(self.schema.synonyms)
    for table in self.tree.find_all(exp.Table):
      name = table.name.upper()
      if not name or name in SKIP_TABLES or name.startswith('__RAW') or (name in self.ctes and not table.db):
        continue
      if self.schema.resolve(name) is None:
        hint = self.similar(name, candidates) or '시노님/다른 스키마라면 DB_SCHEMA 에 추가'
        self.issue('테이블', name, hint)

  def check_columns(self) -> None:
    for col in self.tree.find_all(exp.Column):
      name = col.name.upper()
      if name in ('*', 'NEXTVAL', 'CURRVAL') or name in PSEUDO_COLUMNS or name.startswith('__RAW'):
        continue
      if not col.table and name in self.select_aliases:
        continue
      kind, tables, infos = self.locate(col)
      if kind == 'badalias':
        self.issue('별칭', col.table.upper(), f'정의되지 않은 별칭/테이블 (컬럼 {name})')
      elif kind == 'missing':
        names = sorted({t.name.upper() for t in tables})
        label = f'{names[0]}.{name}' if len(names) == 1 else name
        hint = self.similar(name, {c for i in infos for c in i.columns})
        if len(names) > 1:
          hint = f'조회 대상: {", ".join(names)}' + (f' / {hint}' if hint else '')
        self.issue('컬럼', label, hint)

  def check_insert_columns(self) -> None:
    for ins in self.tree.find_all(exp.Insert):
      target = ins.this
      if not isinstance(target, exp.Schema) or not isinstance(target.this, exp.Table):
        continue
      info = self.schema.resolve(target.this.name)
      if info is None or info.opaque:
        continue
      for ident in target.expressions:
        name = ident.name.upper()
        if name and not name.startswith('__RAW') and name not in info.columns:
          self.issue('컬럼', f'{target.this.name.upper()}.{name}', self.similar(name, info.columns))

  # --- 안티패턴
  def check_patterns(self) -> None:
    for node in self.tree.find_all(exp.Update, exp.Delete):
      if not node.args.get('where'):
        self.add('높음', f'{node.key.upper()} 에 WHERE 없음', '전체 행이 변경/삭제됨. 조건 추가 또는 의도 확인')

    for select in self.tree.find_all(exp.Select):
      if not any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
                 for e in select.expressions):
        continue
      source = select.args.get('from_') or select.args.get('from')
      is_wrapper = source is not None and isinstance(source.this, exp.Subquery)  # 페이징 래퍼 SELECT *
      is_exists = isinstance(select.parent, exp.Subquery) and isinstance(select.parent.parent, exp.Exists)
      if not is_wrapper and not is_exists:
        self.add('낮음', 'SELECT * 사용', '필요한 컬럼만 명시 (불필요한 I/O 감소, 커버링 인덱스 가능, 스키마 변경 영향 감소)')

    for union in self.tree.find_all(exp.Union):
      if union.args.get('distinct'):
        self.add('낮음', 'UNION 사용', '중복이 없거나 허용된다면 UNION ALL (정렬/중복 제거 비용 제거)')

    for not_node in self.tree.find_all(exp.Not):
      if isinstance(not_node.this, exp.In) and not_node.this.args.get('query'):
        self.add('중간', 'NOT IN (서브쿼리)', 'NOT EXISTS 로 변경 (NULL 이 있으면 결과가 비고, 실행계획도 불리)')

  # --- 조건절 분석 (인덱스 활용)
  def conditions(self, node: exp.Expression) -> list[exp.Expression]:
    conds = [node.args.get('where'), node.args.get('on')]
    conds += [j.args.get('on') for j in node.args.get('joins') or []]
    return [c for c in conds if c is not None]

  def check_predicates(self) -> None:
    sargable = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.In, exp.Between)
    wrappers = (exp.Func, exp.Cast, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.DPipe)
    for node in self.tree.find_all(exp.Select, exp.Update, exp.Delete, exp.Merge):
      used: dict[int, tuple[exp.Table, TableInfo, dict[str, None]]] = {}
      for cond in self.conditions(node):
        for pred in cond.find_all(*sargable):
          if owner_of(pred) is not node:
            continue
          leading = isinstance(pred, exp.Like) and _leading_wildcard(pred.expression)
          if leading:
            self.check_leading_wildcard(pred)
          sides = [pred.this, pred.expression] if isinstance(pred, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)) else [pred.this]
          for side in map(_unwrap, sides):
            if isinstance(side, exp.Column):
              kind, tables, infos = self.locate(side)
              if kind == 'found' and not leading:  # 선두 % LIKE 는 인덱스를 못 타므로 인덱스 후보에서 제외
                used.setdefault(id(tables[0]), (tables[0], infos[0], {}))[2][side.name.upper()] = None
            elif isinstance(side, wrappers):
              self.check_wrapped(side)
      for table, info, cols in used.values():
        self.check_index(table, info, list(cols))

  def check_leading_wildcard(self, pred: exp.Like) -> None:
    for col in pred.this.find_all(exp.Column):
      kind, _, infos = self.locate(col)
      if kind != 'found':
        continue
      indexed = col.name.upper() in {c for i in infos[0].indexes.values() for c in i}
      self.add('높음' if indexed else '중간', "LIKE 선두 '%' 패턴",
               f'{pred.sql(dialect="oracle")}: 선두 와일드카드라 인덱스를 사용할 수 없음 '
               '(전방 일치로 변경 또는 Oracle Text 검토)')

  def check_wrapped(self, side: exp.Expression) -> None:
    for col in side.find_all(exp.Column):
      kind, _, infos = self.locate(col)
      if kind != 'found' or col.name.upper() not in {c for i in infos[0].indexes.values() for c in i}:
        continue
      if isinstance(side, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        func, hint = '연산', '연산을 비교 값 쪽으로 이동 (예: 컬럼 + 1 = :v → 컬럼 = :v - 1)'
      else:
        func = re.match(r'\w+', side.sql(dialect='oracle')).group().upper()
        hint = FUNC_HINTS.get(func, DEFAULT_FUNC_HINT)
      self.add('높음', '인덱스 컬럼에 함수/연산 적용',
               f'{side.sql(dialect="oracle")}: {func} 때문에 {col.name.upper()} 인덱스를 사용하지 못함 → {hint}')

  def check_index(self, table: exp.Table, info: TableInfo, cols: list[str]) -> None:
    if info.is_view or info.opaque:
      return
    if any(idx and idx[0] in cols for idx in info.indexes.values()):
      return
    name = table.name.upper()
    existing = '; '.join(f'{n}({", ".join(c)})' for n, c in info.indexes.items()) or '없음'
    suggestion = f'CREATE INDEX IDX_{name}_{"_".join(cols)}'[:30] + f' ON {name} ({", ".join(cols)});'
    self.add('중간', '조건 컬럼에 사용 가능한 인덱스 없음',
             f'{name}: 조건 컬럼 {", ".join(cols)} 이(가) 선두인 인덱스 없음 (기존 인덱스: {existing}). '
             f'후보: `{suggestion}` — 선택도/기존 인덱스와의 중복을 확인하고 컬럼 순서 조정 후 적용')


def analyze_query(query: Query, schema: Schema) -> None:
  if query.dollars:  # SQL 파싱 성공 여부와 무관하게 텍스트만으로 판단 가능
    query.findings.append(Finding(
      '높음', '${} 문자열 치환 사용',
      f'{", ".join(sorted(set(query.dollars)))} → #{{}} 바인드로 변경 (SQL 인젝션, 하드 파싱 유발). '
      '동적 테이블/ORDER BY 는 허용 값 화이트리스트 검증 필요',
    ))
  try:
    tree = sqlglot.parse_one(query.sql, read='oracle')
  except (SqlglotError, RecursionError) as e:
    query.parse_error = str(e).splitlines()[0] if str(e) else type(e).__name__
    return
  try:
    query.pretty = tree.sql(dialect='oracle', pretty=True)
  except SqlglotError:
    query.pretty = query.sql
  try:
    Checker(tree, schema, query).run()
  except Exception as e:  # 한 쿼리의 분석 오류가 전체 실행을 막지 않도록 리포트에 남기고 계속
    query.parse_error = f'분석 중 오류: {type(e).__name__}: {e}'


# ---------------------------------------------------------------- 리포트

def _cell(text: str) -> str:
  return text.replace('|', '\\|').replace('\n', ' ')


def render_report(queries: list[Query], xml_errors: list[tuple[str, str]], owners: list[str],
                  mapper_dir: str, generated_at: datetime | None = None) -> str:
  generated_at = generated_at or datetime.now()
  issues = [(q, i) for q in queries for i in q.issues]
  counts = {s: sum(1 for q in queries for f in q.findings if f.severity == s) for s in SEVERITY_ORDER}
  failed = [q for q in queries if q.parse_error]

  lines = [
    '# MyBatis 쿼리 점검 리포트', '',
    f'- 생성 시각: {generated_at:%Y-%m-%d %H:%M}',
    f'- 대상 스키마: {", ".join(owners)}',
    f'- 매퍼 폴더: `{mapper_dir}`',
    f'- 분석한 쿼리: {len(queries)}개',
    '- 규칙 기반 정적 분석입니다. 실제 성능은 `EXPLAIN PLAN`/실행 통계로 반드시 확인하세요.',
    '- 아래 SQL은 `#{}`→`:b`, `${}`→`__RAW__` 로 치환하고 동적 SQL(`<if>` 등)을 모두 참으로 가정해 펼친 것입니다.',
    '', '## 요약', '',
    '| 구분 | 건수 |', '|---|---|',
    f'| 스키마 불일치 (없는 테이블/컬럼/별칭) | {len(issues)} |',
    *[f'| 개선 제안 - {s} | {counts[s]} |' for s in SEVERITY_ORDER],
    f'| 분석 실패 (SQL/XML) | {len(failed) + len(xml_errors)} |',
    '', '## 1. 스키마 불일치', '',
  ]

  if issues:
    lines += ['| 매퍼 | 쿼리 | 구분 | 이름 | 비고 |', '|---|---|---|---|---|']
    lines += [f'| {_cell(q.file)} | {_cell(q.full_id)} | {i.kind} | `{_cell(i.name)}` | {_cell(i.hint)} |'
              for q, i in issues]
  else:
    lines.append('없음')

  lines += ['', '## 2. 개선 제안', '']
  reviewed = [q for q in queries if q.findings]
  if not reviewed:
    lines.append('없음')
  current_file = None
  for q in reviewed:
    if q.file != current_file:
      current_file = q.file
      lines += [f'### {q.file}', '']
    lines += [f'#### `{q.full_id}` ({q.kind})', '']
    for f in sorted(q.findings, key=lambda f: SEVERITY_ORDER.index(f.severity)):
      lines.append(f'- **[{f.severity}] {f.title}** — {f.detail}')
    lines += ['', '```sql', q.pretty or q.sql, '```', '']

  lines += ['## 3. 분석 실패 (수동 확인 필요)', '']
  if failed or xml_errors:
    lines += [f'- XML `{f}`: {e}' for f, e in xml_errors]
    lines += [f'- `{q.file}` `{q.full_id}`: {q.parse_error}' for q in failed]
  else:
    lines.append('없음')
  return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------- 실행

def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description='MyBatis 쿼리를 Oracle DDL과 대조해 개선 리포트를 생성합니다.')
  parser.add_argument('mapper_dir', nargs='?', help='매퍼 XML 폴더 (생략 시 .env 의 MAPPER_DIR)')
  parser.add_argument('--env', default='.env', help='.env 파일 경로')
  parser.add_argument('--out-dir', default=str(Path(__file__).parent / 'docs'), help='리포트 출력 폴더')
  args = parser.parse_args(argv)

  cfg = {**dotenv_values(args.env), **os.environ}
  missing = [k for k in ('DB_USER', 'DB_PASSWORD', 'DB_DSN') if not cfg.get(k)]
  if missing:
    print(f'{args.env} 에 필요한 값이 없습니다: {", ".join(missing)}', file=sys.stderr)
    return 1

  mapper_dir = Path(args.mapper_dir or cfg.get('MAPPER_DIR') or '')
  if not mapper_dir.is_dir():
    print(f'매퍼 폴더를 찾을 수 없습니다: "{mapper_dir}" (인자 또는 MAPPER_DIR 지정)', file=sys.stderr)
    return 1

  owners = [o.strip().upper() for o in (cfg.get('DB_SCHEMA') or cfg['DB_USER']).split(',') if o.strip()]
  try:
    with oracledb.connect(user=cfg['DB_USER'], password=cfg['DB_PASSWORD'], dsn=cfg['DB_DSN']) as conn:
      schema = load_schema(conn, owners)
  except oracledb.Error as e:
    print(f'Oracle 접속/조회 실패: {e}', file=sys.stderr)
    return 1
  if not schema.tables:
    print(f'스키마 {", ".join(owners)} 에서 테이블을 찾지 못했습니다. DB_SCHEMA 와 권한을 확인하세요.', file=sys.stderr)
    return 1

  queries, xml_errors = collect_queries(mapper_dir)
  for query in queries:
    analyze_query(query, schema)

  now = datetime.now()
  out = Path(args.out_dir) / f'query-review-{now:%Y%m%d-%H%M%S}.md'
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(render_report(queries, xml_errors, owners, str(mapper_dir), now), encoding='utf-8')
  print(f'쿼리 {len(queries)}개 분석 완료 -> {out}')
  return 0


if __name__ == '__main__':
  sys.exit(main())
