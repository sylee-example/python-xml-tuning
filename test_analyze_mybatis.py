"""analyze_mybatis 테스트. DB 없이 가짜 Schema 와 임시 매퍼 XML 로 검증한다."""
from pathlib import Path

import pytest

from analyze_mybatis import (
  Schema, TableInfo, analyze_query, collect_queries, render_report,
)

MAPPER_HEAD = (
  '<?xml version="1.0" encoding="UTF-8"?>\n'
  '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" '
  '"http://mybatis.org/dtd/mybatis-3-mapper.dtd">\n'
)


@pytest.fixture
def schema() -> Schema:
  return Schema(
    tables={
      'EMP': TableInfo(
        columns={'ID', 'NAME', 'DEPT_ID', 'STATUS', 'HIRE_DATE'},
        indexes={'PK_EMP': ['ID'], 'IDX_EMP_DEPT': ['DEPT_ID'], 'IDX_EMP_NAME': ['NAME']},
      ),
      'DEPT': TableInfo(columns={'ID', 'NM'}, indexes={'PK_DEPT': ['ID']}),
      'ORDERS': TableInfo(columns={'ID', 'EMP_ID', 'AMT'}),
      'V_EMP': TableInfo(columns={'ID', 'NAME'}, is_view=True),
    },
    synonyms={'SYN_EMP': 'EMP', 'SYN_REMOTE': 'REMOTE_T'},
  )


def run(tmp_path: Path, body: str, schema: Schema, encoding: str = 'utf-8', head: str = MAPPER_HEAD):
  (tmp_path / 'SampleMapper.xml').write_bytes((head + f'<mapper namespace="sample">\n{body}\n</mapper>').encode(encoding))
  queries, errors = collect_queries(tmp_path)
  assert not errors
  for q in queries:
    analyze_query(q, schema)
  return {q.id: q for q in queries}


def titles(q) -> set[str]:
  return {f.title for f in q.findings}


def issues(q) -> set[tuple[str, str]]:
  return {(i.kind, i.name) for i in q.issues}


# ---------------------------------------------------------------- 동적 SQL 펼치기

def test_dynamic_sql_is_flattened(tmp_path, schema):
  body = '''
  <sql id="cols">ID, NAME</sql>
  <select id="list">
    SELECT <include refid="cols"/> FROM EMP
    <where>
      <if test="name != null">AND NAME = #{name}</if>
      <if test="ids != null">AND ID IN <foreach collection="ids" item="i" open="(" separator="," close=")">#{i}</foreach></if>
    </where>
  </select>
  <update id="upd">
    UPDATE EMP <set><if test="a">NAME = #{name},</if><if test="b">STATUS = #{status},</if></set> WHERE ID = #{id}
  </update>
  <select id="pick">
    SELECT ID FROM EMP WHERE 1 = 1
    <choose><when test="x">AND NAME = #{x}</when><otherwise>AND STATUS = 'A'</otherwise></choose>
  </select>
  '''
  qs = run(tmp_path, body, schema)
  assert qs['list'].sql == 'SELECT ID, NAME FROM EMP WHERE NAME = :b AND ID IN (:b)'
  assert qs['upd'].sql == 'UPDATE EMP SET NAME = :b,STATUS = :b WHERE ID = :b'  # <if> 조각 사이 공백은 없어도 유효한 SQL
  assert qs['pick'].sql == 'SELECT ID FROM EMP WHERE 1 = 1 AND NAME = :b'
  assert not any(q.parse_error for q in qs.values())


def test_cdata_and_line_comment(tmp_path, schema):
  body = '''<select id="c">
    SELECT ID -- 주석
    FROM EMP WHERE HIRE_DATE <![CDATA[ < ]]> SYSDATE
  </select>'''
  assert run(tmp_path, body, schema)['c'].sql == 'SELECT ID FROM EMP WHERE HIRE_DATE < SYSDATE'


def test_euc_kr_mapper_is_readable(tmp_path, schema):
  head = '<?xml version="1.0" encoding="EUC-KR"?>\n'
  body = '<select id="k">SELECT ID FROM EMP WHERE NAME = \'홍길동\'</select>'
  q = run(tmp_path, body, schema, encoding='cp949', head=head)['k']
  assert '홍길동' in q.sql and not q.parse_error


# ---------------------------------------------------------------- 스키마 불일치

def test_missing_table_with_suggestion(tmp_path, schema):
  q = run(tmp_path, '<select id="t">SELECT ID FROM EMPP</select>', schema)['t']
  assert ('테이블', 'EMPP') in issues(q)
  assert 'EMP' in q.issues[0].hint


def test_missing_columns_qualified_unqualified_insert_update(tmp_path, schema):
  body = '''
  <select id="sel">SELECT e.NAMEE, d.NM, STATUSS FROM EMP e JOIN DEPT d ON e.DEPT_ID = d.ID</select>
  <insert id="ins">INSERT INTO EMP (ID, NAME, ZZZ) VALUES (#{id}, #{name}, #{z})</insert>
  <update id="upd">UPDATE EMP e SET e.NOPE = #{v} WHERE e.ID = #{id}</update>
  <select id="alias">SELECT x.ID FROM EMP e</select>
  '''
  qs = run(tmp_path, body, schema)
  assert ('컬럼', 'EMP.NAMEE') in issues(qs['sel'])
  assert any(i.kind == '컬럼' and i.name == 'STATUSS' for i in qs['sel'].issues)
  assert ('컬럼', 'DEPT.NM') not in issues(qs['sel'])
  assert issues(qs['ins']) == {('컬럼', 'EMP.ZZZ')}
  assert issues(qs['upd']) == {('컬럼', 'EMP.NOPE')}
  assert ('별칭', 'X') in issues(qs['alias'])


def test_no_false_positives_on_valid_complex_queries(tmp_path, schema):
  body = '''
  <select id="ok1">
    WITH t AS (SELECT ID FROM EMP WHERE STATUS = #{s})
    SELECT t.ID, v.cnt, x.NM
    FROM t
    JOIN (SELECT EMP_ID, COUNT(*) cnt FROM ORDERS WHERE EMP_ID = #{e} GROUP BY EMP_ID) v ON v.EMP_ID = t.ID
    JOIN DEPT x ON x.ID = t.ID
    ORDER BY cnt
  </select>
  <select id="ok2">
    SELECT a.ID FROM EMP a
    WHERE EXISTS (SELECT 1 FROM EMP a WHERE a.NAME = #{n} AND a.DEPT_ID = #{d})
      AND ROWNUM &lt;= 10 AND a.ID = #{id}
  </select>
  <select id="ok3">SELECT NAME total FROM SYN_EMP WHERE ID = #{id} ORDER BY total</select>
  <select id="ok4">SELECT ID FROM SYN_REMOTE WHERE ANYTHING = #{x}</select>
  <insert id="ok5">INSERT INTO EMP (ID, NAME) VALUES (SEQ_EMP.NEXTVAL, #{name})</insert>
  <select id="ok6">SELECT ID FROM DUAL WHERE 1 = 1</select>
  '''
  for q in run(tmp_path, body, schema).values():
    assert not q.issues, (q.id, q.issues)
    assert not q.parse_error, (q.id, q.parse_error)


# ---------------------------------------------------------------- 개선 규칙

def test_antipatterns(tmp_path, schema):
  body = '''
  <select id="star">SELECT * FROM EMP WHERE ID = #{id}</select>
  <select id="wrapper">SELECT * FROM (SELECT ID FROM EMP WHERE ID = #{id}) WHERE ROWNUM &lt;= 10</select>
  <select id="dollar">SELECT ID FROM EMP WHERE ID = #{id} ORDER BY ${col}</select>
  <select id="like1">SELECT ID FROM EMP WHERE NAME LIKE '%' || #{n} || '%'</select>
  <select id="like2">SELECT ID FROM EMP WHERE NAME LIKE #{n} || '%'</select>
  <select id="like3">SELECT ID FROM EMP WHERE UPPER(NAME) LIKE '%' || #{n}</select>
  <select id="func">SELECT ID FROM EMP WHERE TRUNC(HIRE_DATE) = TRUNC(SYSDATE) AND UPPER(NAME) = #{n}</select>
  <select id="arith">SELECT ID FROM EMP WHERE ID + 1 = #{id}</select>
  <delete id="del">DELETE FROM EMP</delete>
  <update id="upd">UPDATE EMP SET NAME = #{n}</update>
  <select id="union">SELECT ID FROM EMP UNION SELECT ID FROM DEPT</select>
  <select id="unionall">SELECT ID FROM EMP UNION ALL SELECT ID FROM DEPT</select>
  <select id="notin">SELECT ID FROM EMP WHERE DEPT_ID NOT IN (SELECT ID FROM DEPT)</select>
  '''
  qs = run(tmp_path, body, schema)
  assert 'SELECT * 사용' in titles(qs['star'])
  assert 'SELECT * 사용' not in titles(qs['wrapper'])
  assert '${} 문자열 치환 사용' in titles(qs['dollar'])
  assert "LIKE 선두 '%' 패턴" in titles(qs['like1'])
  assert "LIKE 선두 '%' 패턴" not in titles(qs['like2'])
  assert "LIKE 선두 '%' 패턴" in titles(qs['like3'])  # 좌변이 함수여도 잡아야 함 (회귀)
  assert titles(qs['func']) == {'인덱스 컬럼에 함수/연산 적용'}
  assert len(qs['func'].findings) == 1  # HIRE_DATE 는 인덱스가 없어 UPPER(NAME) 만 해당
  assert '인덱스 컬럼에 함수/연산 적용' in titles(qs['arith'])
  assert 'DELETE 에 WHERE 없음' in titles(qs['del'])
  assert 'UPDATE 에 WHERE 없음' in titles(qs['upd'])
  assert 'UNION 사용' in titles(qs['union'])
  assert 'UNION 사용' not in titles(qs['unionall'])
  assert 'NOT IN (서브쿼리)' in titles(qs['notin'])


def test_index_suggestions(tmp_path, schema):
  body = '''
  <select id="noidx">SELECT ID FROM ORDERS WHERE EMP_ID = #{e} AND AMT &gt; #{a}</select>
  <select id="hasidx">SELECT ID FROM EMP WHERE DEPT_ID = #{d}</select>
  <select id="join">
    SELECT o.ID FROM ORDERS o JOIN EMP e ON e.ID = o.EMP_ID WHERE e.DEPT_ID = #{d}
  </select>
  <select id="view">SELECT ID FROM V_EMP WHERE NAME = #{n}</select>
  <select id="nonleading">SELECT ID FROM EMP WHERE STATUS = #{s}</select>
  '''
  qs = run(tmp_path, body, schema)
  noidx = qs['noidx'].findings
  assert len(noidx) == 1 and 'EMP_ID, AMT' in noidx[0].detail and 'CREATE INDEX' in noidx[0].detail
  assert not qs['hasidx'].findings
  # ORDERS 는 조인 키(EMP_ID)에 인덱스가 없으므로 지적, EMP 는 PK/DEPT_ID 인덱스가 있어 통과
  assert [f.detail.split(':')[0] for f in qs['join'].findings] == ['ORDERS']
  assert not qs['view'].findings  # 뷰는 인덱스 분석 제외
  assert 'EMP: 조건 컬럼 STATUS' in qs['nonleading'].findings[0].detail


def test_parse_failure_is_reported_not_raised(tmp_path, schema):
  q = run(tmp_path, '<update id="plsql">BEGIN PKG.DO_IT(#{id}); END;</update>', schema)['plsql']
  assert q.parse_error


def test_report_contains_schema_issues_and_findings(tmp_path, schema):
  body = '''
  <select id="bad">SELECT e.NAMEE FROM EMPP e</select>
  <select id="slow">SELECT * FROM EMP WHERE ID = #{id}</select>
  <select id="fine">SELECT ID FROM EMP WHERE ID = #{id}</select>
  '''
  qs = run(tmp_path, body, schema)
  report = render_report(list(qs.values()), [], ['HR'], str(tmp_path))
  schema_section = report.split('## 2.')[0]
  assert '`EMPP`' in schema_section and 'sample.bad' in schema_section
  assert 'SELECT * 사용' in report.split('## 2.')[1]
  assert 'sample.fine' not in report


# ---------------------------------------------------------------- DB 읽기 / main (가짜 커넥션)

class FakeCursor:
  ROWS = {
    'all_tab_columns': [('EMP', 'ID'), ('EMP', 'NAME'), ('V_EMP', 'ID')],
    'all_views': [('V_EMP',)],
    'all_ind_columns': [('EMP', 'PK_EMP', 'ID'), ('EMP', 'IDX_NAME', 'NAME')],
    'all_synonyms': [('SYN_EMP', 'EMP')],
  }

  def __init__(self, calls):
    self.calls, self.rows = calls, []

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False

  def execute(self, sql, binds):
    self.calls.append((sql, binds))
    self.rows = next(rows for key, rows in self.ROWS.items() if key in sql)

  def __iter__(self):
    return iter(self.rows)


class FakeConn:
  def __init__(self):
    self.calls = []

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False

  def cursor(self):
    return FakeCursor(self.calls)


def test_load_schema_builds_tables_indexes_views_synonyms():
  from analyze_mybatis import load_schema
  conn = FakeConn()
  schema = load_schema(conn, ['HR', 'APP'])
  assert schema.tables['EMP'].columns == {'ID', 'NAME'}
  assert schema.tables['EMP'].indexes == {'PK_EMP': ['ID'], 'IDX_NAME': ['NAME']}
  assert schema.tables['V_EMP'].is_view and not schema.tables['EMP'].is_view
  assert schema.resolve('syn_emp') is schema.tables['EMP']
  assert all(binds == {'o0': 'HR', 'o1': 'APP'} for _, binds in conn.calls)  # 스키마명은 바인드 변수로 전달


def test_main_end_to_end_writes_report_to_docs(tmp_path, monkeypatch):
  import analyze_mybatis
  from analyze_mybatis import main
  mapper_dir = tmp_path / 'mappers'
  mapper_dir.mkdir()
  (mapper_dir / 'A.xml').write_text(
    MAPPER_HEAD + '<mapper namespace="a"><select id="x">SELECT NOPE FROM EMP WHERE NAME = #{n}</select></mapper>',
    encoding='utf-8',
  )
  env = tmp_path / '.env'
  env.write_text("DB_USER=hr\nDB_PASSWORD='p#ss'\nDB_DSN=host:1521/svc\n", encoding='utf-8')
  seen = {}

  def fake_connect(**kwargs):
    seen.update(kwargs)
    return FakeConn()

  monkeypatch.setattr(analyze_mybatis.oracledb, 'connect', fake_connect)
  assert main([str(mapper_dir), '--env', str(env), '--out-dir', str(tmp_path / 'docs')]) == 0
  assert seen == {'user': 'hr', 'password': 'p#ss', 'dsn': 'host:1521/svc'}
  (report_file,) = (tmp_path / 'docs').glob('query-review-*.md')
  text = report_file.read_text(encoding='utf-8')
  assert '`EMP.NOPE`' in text and '대상 스키마: HR' in text


def test_main_fails_cleanly_without_env(tmp_path, capsys, monkeypatch):
  from analyze_mybatis import main
  for key in ('DB_USER', 'DB_PASSWORD', 'DB_DSN'):
    monkeypatch.delenv(key, raising=False)
  assert main([str(tmp_path), '--env', str(tmp_path / 'none.env')]) == 1
  assert 'DB_USER' in capsys.readouterr().err
