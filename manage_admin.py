"""Run locally to change the administrator password without exposing it in arguments."""
import getpass
from pathlib import Path
import server

def main():
    server.init_db()
    password=getpass.getpass('새 관리자 비밀번호 (12자 이상): ')
    again=getpass.getpass('새 비밀번호 확인: ')
    if len(password)<12 or password!=again:
        raise SystemExit('비밀번호 길이 또는 확인 값이 올바르지 않습니다. 변경하지 않았습니다.')
    with server.connect() as c:
        c.execute("UPDATE config SET value=? WHERE key='admin_hash'",(server.hash_password(password),))
        c.execute('DELETE FROM sessions WHERE admin=1')
    initial=server.RUNTIME/'admin-access.txt'
    if initial.exists():initial.unlink()
    print('비밀번호를 변경하고 기존 관리자 세션을 종료했습니다.')
if __name__=='__main__':main()
