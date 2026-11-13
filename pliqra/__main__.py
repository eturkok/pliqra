from .config import env
import argparse
import getpass
import os
import time
from pathlib import Path


def main():
    import sys
    if sys.argv[1:2] == ['policy']:
        from .policy_cli import main as policy_main
        raise SystemExit(policy_main(sys.argv[2:]))
    parser=argparse.ArgumentParser(description='Pliqra gateway and offline recovery tools', epilog='Policy files: python -m pliqra policy --help')
    parser.add_argument('command',nargs='?',default='serve',choices=['serve','check','backup','restore','reset-password'])
    parser.add_argument('--file',help='Backup archive input/output path')
    parser.add_argument('--email',help='Local account to recover')
    parser.add_argument('--logical',action='store_true',help='Create a portable logical backup for PostgreSQL migration')
    args=parser.parse_args()
    if args.command=='serve':
        import uvicorn
        certificate=env('PLIQRA_TLS_CERTFILE')
        private_key=env('PLIQRA_TLS_KEYFILE')
        if bool(certificate)!=bool(private_key):parser.error('Set both PLIQRA_TLS_CERTFILE and PLIQRA_TLS_KEYFILE')
        uvicorn.run('pliqra.main:create_app',factory=True,host=env('PLIQRA_HOST','0.0.0.0'),port=int(env('PLIQRA_PORT','8080')),proxy_headers=False,access_log=False,timeout_graceful_shutdown=30,ssl_certfile=certificate,ssl_keyfile=private_key)
        return
    from .db import Store
    from .security import Vault,hash_password
    from .operations import backup_bytes,restore_bytes
    directory=Path(env('PLIQRA_DATA_DIR','data'))
    try:store=Store(directory)
    except RuntimeError as exc:raise SystemExit(str(exc)+' Stop the gateway before running offline maintenance.')
    try:
        vault=Vault(directory,store)
        if args.command=='check':
            result=store.one('PRAGMA integrity_check') if store.backend=='sqlite' else {'integrity_check':'ok' if store.one('SELECT 1 AS connected')['connected']==1 else 'error'}
            if next(iter(result.values()))!='ok':raise SystemExit('Database integrity check failed')
            print('Database integrity and stored credential encryption verified.')
        elif args.command=='backup':
            if not args.file:parser.error('--file is required')
            password=getpass.getpass('Backup password (at least 12 characters): ')
            if len(password)<12 or password!=getpass.getpass('Confirm backup password: '):raise SystemExit('Passwords must match and contain at least 12 characters')
            if args.logical:
                from .logical_archive import backup
                archive=backup(store,vault,password)
            else:archive=backup_bytes(store,vault,password)
            Path(args.file).write_bytes(archive)
            print('Encrypted backup saved.')
        elif args.command=='restore':
            if not args.file:parser.error('--file is required')
            if input('Type RESTORE to replace this workspace: ')!='RESTORE':raise SystemExit('Cancelled')
            password=getpass.getpass('Backup password: ')
            if store.backend=='postgresql' and store.one('SELECT id FROM runtime_nodes WHERE heartbeat>?',(time.time()-60,)):
                raise SystemExit('Stop every workspace replica before offline restore')
            restore_bytes(store,vault,Path(args.file).read_bytes(),password)
            print('Backup restored. Start the gateway and sign in again.')
        else:
            if not args.email:parser.error('--email is required')
            user=store.one("SELECT id FROM users WHERE issuer='local' AND subject=?",(args.email.strip().lower(),))
            if not user:raise SystemExit('Local account not found')
            password=getpass.getpass('New password (at least 12 characters): ')
            if len(password)<12 or password!=getpass.getpass('Confirm new password: '):raise SystemExit('Passwords must match and contain at least 12 characters')
            with store.transaction():
                store.update('users',user['id'],{'password_hash':hash_password(password),'active':1})
                store.run('DELETE FROM sessions WHERE user_id=?',(user['id'],))
                store.audit('offline-recovery','password.reset',user['id'])
            print('Local password changed; existing sessions revoked.')
    finally:store.close()


if __name__=='__main__':main()
