#!/usr/bin/env python3
"""在隔离 Linux 容器中调用实际生产 LDAP 实现，不模拟协议或返回值。"""
import json
import sys
sys.path.insert(0, '/manager')
from access import ldap_directory
from access.errors import AccessError

request = json.load(sys.stdin)
try:
    action = request['action']
    config = request['config']
    if action == 'authenticate':
        result = ldap_directory.authenticate(config, request['username'], request['password'])
    elif action == 'search_users':
        users = ldap_directory.search_users(config, request.get('query', ''))
        result = {'count': len(users), 'subjects': len({row['subject'] for row in users})}
    elif action == 'revalidate_disabled':
        users = ldap_directory.search_users(config, 'user0002')
        if not users:
            raise AccessError('禁用目录账号已被过滤', 401)
        result = ldap_directory.revalidate(config, 'user0002', users[0]['subject'])
    elif action == 'identity_lifecycle':
        import ldap3
        config = ldap_directory.validate_config(config)
        first = ldap_directory.authenticate(config, 'user1499', request['password'])
        with ldap_directory.connection(config) as conn:
            conn.rebind(user='cn=admin,dc=access,dc=test', password=request['password'], authentication=ldap3.SIMPLE)
            old_dn = 'uid=user1499,ou=people,dc=access,dc=test'
            new_dn = 'uid=renamed1499,ou=people,dc=access,dc=test'
            conn.modify_dn(old_dn, 'uid=renamed1499')
            renamed = ldap_directory.authenticate(config, 'renamed1499', request['password'])
            conn.modify_dn(new_dn, 'uid=user1499')
            conn.delete(old_dn)
            conn.add(old_dn, ['inetOrgPerson'], {'uid':'user1499','cn':'user1499','sn':'1499',
                'givenName':'测试','mail':'user1499@access.test','employeeNumber':'user1499',
                'description':'enabled','userPassword':request['password']})
            rebuilt = ldap_directory.authenticate(config, 'user1499', request['password'])
        result = {'renamed_stable': first['subject']==renamed['subject'], 'recreated_new':first['subject']!=rebuilt['subject']}
    elif action == 'revalidate':
        result = ldap_directory.revalidate(config, request['username'], request['subject'])
    elif action in ('attribute_fixture_create','attribute_fixture_delete'):
        import ldap3
        config = ldap_directory.validate_config(config)
        with ldap_directory.connection(config) as conn:
            conn.rebind(user='cn=admin,dc=access,dc=test', password=request['password'], authentication=ldap3.SIMPLE)
            dn='uid='+request['uid']+',ou=people,dc=access,dc=test'
            if action == 'attribute_fixture_create':
                result=conn.add(dn,['inetOrgPerson'],{'uid':request['uid'],'cn':request['username'],'sn':'属性校验',
                    'givenName':'公开姓名','employeeNumber':'私有姓名','mail':request['uid']+'@access.test',
                    'description':'enabled','userPassword':request['user_password']})
            else:
                result=conn.delete(dn)
    elif action in ('session_fixture_rename','session_fixture_recreate'):
        import ldap3
        config=ldap_directory.validate_config(config)
        with ldap_directory.connection(config) as conn:
            conn.rebind(user='cn=admin,dc=access,dc=test',password=request['password'],authentication=ldap3.SIMPLE)
            # 仅会话验收保留账号，禁止对任意传入 DN 执行变更。
            if action=='session_fixture_rename':
                old='renamed0010' if request.get('restore') else 'user0010'
                new='user0010' if request.get('restore') else 'renamed0010'
                result=conn.modify_dn('uid='+old+',ou=people,dc=access,dc=test','uid='+new)
            else:
                dn='uid=user0010,ou=people,dc=access,dc=test'
                conn.delete(dn)
                result=conn.add(dn,['inetOrgPerson'],{'uid':'user0010','cn':'user0010','sn':'10',
                    'givenName':'测试','employeeNumber':'user0010','mail':'user0010@access.test',
                    'description':'enabled','userPassword':request['password']})
    else:
        result = ldap_directory.test_connection(config)
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
except AccessError as exc:
    print(json.dumps({'ok': False, 'message': str(exc), 'status': exc.status}, ensure_ascii=False))
