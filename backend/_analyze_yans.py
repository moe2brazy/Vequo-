# -*- coding: utf-8 -*-
import sys, json
sys.path.insert(0, '.')
from database import switch_database
switch_database({"db_type":"postgresql","host":"localhost","port":5432,"name":"yans","user":"postgres","password":"123456"})
from database import engine
from sqlalchemy import text

def q(sql):
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text(sql)).fetchall()]

# 1) 表清单 + 行数
print("═══ 表清单 ═══")
tables = q("""SELECT table_name FROM information_schema.tables
              WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name""")
for (t,) in tables:
    try:
        n = q(f"SELECT COUNT(*) FROM {t}")[0][0]
    except Exception as e:
        n = f"ERR {e}"
    print(f"  {t}: {n} 行")

# 2) 每表字段（含主键/外键信息）
print("\n═══ 字段明细（PK/FK/非空率/取值）═══")
fk_q = """SELECT tc.table_name, kcu.column_name, ccu.table_name AS ref_table, ccu.column_name AS ref_col
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
          JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.table_schema=tc.table_schema
          WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'"""
fks = {}
for t, col, rt, rc in q(fk_q):
    fks.setdefault(t, []).append(f"{col}→{rt}.{rc}")

for (t,) in tables:
    print(f"\n── {t} (FK: {fks.get(t, '无')}) ──")
    cols = q(f"""SELECT column_name, data_type, is_nullable FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='{t}' ORDER BY ordinal_position""")
    pk = {r[0] for r in q(f"""SELECT kcu.column_name FROM information_schema.table_constraints tc
                 JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
                 WHERE tc.table_name='{t}' AND tc.constraint_type='PRIMARY KEY'""")}
    for name, dtype, nullable in cols:
        mark = "PK" if name in pk else ""
        # 非空率 + 样例
        try:
            ntot = q(f"SELECT COUNT(*) FROM {t}")[0][0]
            nn = q(f"SELECT COUNT({name}) FROM {t}")[0][0] if ntot else 0
            nnr = f"{nn}/{ntot}" if ntot else "0/0"
        except Exception:
            nnr = "?"
        print(f"    {name} {dtype} {mark} 非空={nnr}")
