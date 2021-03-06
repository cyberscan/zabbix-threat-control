#!/usr/bin/env python3
"""Zabbix vulnerability assessment plugin."""

import argparse
import json
import os
import pickle
import re
import subprocess
from datetime import datetime
from statistics import mean, median
from time import sleep

import jpath

from pyzabbix import ZabbixAPI

import requests

import ztc_config as c


ztc_version = 0.2
vulners_url = 'https://vulners.com/api/v3/audit/audit/'
jpath_mask = 'data.packages.*.*.*'
item_key = 'system.run[{$REPORT_SCRIPT_PATH} package]'


parser = argparse.ArgumentParser(description='Vulners to zabbix integration tool')


parser.add_argument(
    '--BypassZbxPush',
    help='Bypass Zabbix-server. Don\'t push final dataset to Zabbix-server.',
    action='store_true')


parser.add_argument(
    '--DumpHostMatrix',
    help='Dump zabbix and vulners data to disk',
    action='store_true')


args = parser.parse_args()


# логирование, если передать второй аргумент то дописывает в послднюю строку
def logw(text, option='normal'):
    f = open(c.log_file, 'a')
    if option == 'normal':
        # now = datetime.now()
        # text = f'\n{now:%Y-%m-%d %H:%M:%S} {text}'
        text = '\n' + datetime.now().strftime('%Y-%m-%d %H:%M:%S ') + text
    f.write(text)
    f.close()


# выполение комадн в шелле
def shell(command):
    proc = subprocess.Popen(command, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, shell=True)
    out = proc.communicate()[0].decode('utf8')
    return out


# аналог gnu-утилиты unic (оставляем только уникальные записи в списке)
def uniq_list(l):
    x = []
    for a in l:
        if a not in x:
            x.append(a)
    return x


# проверяем валидность информации об ОС
def os_data_valid(os, version, packages, host_name):
    try:
        if len(os) > 0 and float(version) != 0 and len(packages) > 5:
            return True
    except Exception as e:
        logw('Excluded {host_name}. Exception: {e}'.format(host_name=host_name, e=e))
        return False
    logw('Excluded {host_name}. ОS: {os}, Version: {version}, Packages: {pkgs_len}'
         .format(host_name=host_name, os=os, version=version, pkgs_len=len(packages)))
    return False


def dump_create(filename, obj):
    with open(filename, 'wb') as file:
        pickle.dump(obj, file)
    return True


def dump_load(filename):
    with open(filename, 'rb') as file:
        obj = pickle.load(file)
    return obj


logw('Start.')
if len(c.vuln_api_key) != 64:
    logw('Error: not a valid Vulners API-key.')
    exit(1)

# создаем сессию в заббикс
try:
    zapi = ZabbixAPI(c.zbx_url, timeout=10)
    zapi.session.verify = c.zbx_verify_ssl_certs
    zapi.login(c.zbx_user, c.zbx_pass)
    logw('Connected to Zabbix API v.{zapi_ver}'.format(zapi_ver=zapi.api_version()))
except Exception as e:
    logw('Error: Can\'t connect to Zabbix API. Exception: {e}'.format(e=e))
    exit(1)

# Если матрица хостов есть - загружаем дамп с диска
if os.path.exists(c.h_matrix_dumpfile):
    logw('Found a dump of the h_matrix in {h_matrix_dumpfile}. Loading.'.format(h_matrix_dumpfile=c.h_matrix_dumpfile))
    h_matrix = dump_load(c.h_matrix_dumpfile)
    total_hosts = len(h_matrix)
else:
        # если дампа матрицы на диске нет - формируем (исходные данные из zabbix и затем обогащаем их через vulners)
    total_hosts = 0
    try:
        h_matrix = zapi.item.get(search={'key_': item_key}, monitored=True, output=['hostid'])
        full_hosts = len(h_matrix)
        logw('Received from Zabbix {full_hosts} hosts for processing.'.format(full_hosts=full_hosts))
    except Exception as e:
        logw('Error: Can\'t get data from Zabbix. Exception: {e}'.format(e=e))
        exit()

    logw('Receiving additional information for all hosts from Zabbix')
    current_host = 0
    logw('Processed hosts')
    for h in h_matrix:
        current_host += 1
        try:
            z = zapi.host.get(filter={'hostid': h['hostid']},
                              output=['host', 'name'],
                              selectInventory=['os', 'os_full', 'software_full'])

            # обновляем строку в матрице (пишем в матрицу полученные данные)
            h.update({'software_full': z[0]['inventory']['software_full'].splitlines(),
                      # 'os':          z[0]['inventory']['os'],
                      'os': re.sub('ol', 'oraclelinux', z[0]['inventory']['os']),
                      'version': z[0]['inventory']['os_full'],
                      'v_name': z[0]['name'],
                      # 'h_ip':          z[0]['interfaces'][0]['ip'],
                      'host_name': z[0]['host']})

            logw('.', 0)
        except Exception as e:
            logw('[{current_host} in {full_hosts}] Skip, can\'t get additional data from Zabbix. Exception: {e}'
                 .format(current_host=current_host, full_hosts=full_hosts, e=e))
            h.update({'software_full': '',
                      'os': '',
                      'version': '',
                      'v_name': '',
                      'host_name': ''})
            continue
    logw(' total: {current_host}.'.format(current_host=current_host), 0)

    logw('Checking data from Zabbix.')
    # удаляем невалидные элементы данных из матрицы (там где триплет с хоста не подходящй)
    h_matrix[:] = [h for h in h_matrix if os_data_valid(h['os'], h['version'], h['software_full'], h['v_name'])]
    total_hosts = len(h_matrix)
    removed_cnt = full_hosts - total_hosts
    logw('After checking data from Zabbix, there are {total_hosts} entries left. Removed {removed_cnt}.'
         .format(total_hosts=total_hosts, removed_cnt=removed_cnt))

    # обогащаем матрицу данными от вулнерса
    logw('Receiving the vulnerabilities from Vulners.')
    current_host = 0
    logw('Processed hosts')
    user_agent = 'vulners-ztc-{ztc_version}'.format(ztc_version=ztc_version)
    for h in h_matrix:
        current_host += 1
        try:
            os_data = '{"package":' + json.dumps(h['software_full']) + ',"os":"' + h['os'] + \
                      '","version":"' + h['version'] + '","apiKey":"' + c.vuln_api_key + '"}'
            # идем в вулнерс и получем там уязвимости для списка пакетов и ОС
            vuln_response = requests.post(vulners_url, data=os_data, headers={'User-Agent': user_agent,
                                                                              'Content-Type': 'application/json', })
            h.update({'vuln_data': vuln_response.json()})
            # чтобы вулнерс не упал под нагрузкой, засыпаем на чуть-чуть (между запросами)
            ratelimit = int(vuln_response.headers.get('x-vulners-ratelimit-reqlimit'))
            sleep_timeout = 2 / ratelimit
            sleep(sleep_timeout)
            logw('.', 0)
        except Exception as e:
            sleep_timeout = 2
            h.update({'vuln_data': {'result': 'FAIL'}})
            logw('[{current_host} in {total_hosts}] Skip {v_name}], can\'t receive the vulnerabilities from Vulners. Exception: {e}'
                 .format(current_host=current_host, total_hosts=total_hosts, v_name=h["v_name"], e=e))
            continue
    logw(' total: {current_host}.'.format(current_host=current_host), 0)

    logw('Checking data from Vulners.')
    # удаляем невалидные элементы данных из матрицы (там где ответ вулнерс не подходящй)
    h_matrix[:] = [h for h in h_matrix if h['vuln_data']['result'] == 'OK']
    total_hosts = len(h_matrix)
    logw('After checking data from Vulners there are {total_hosts} entries left.'.format(total_hosts=total_hosts))

    if args.DumpHostMatrix:
        try:
            # сохраняем дамп матрицы хостов на диск
            dump_create(c.h_matrix_dumpfile, h_matrix)
            logw('host-matrix saved to {h_matrix_dumpfile}.'.format(h_matrix_dumpfile=c.h_matrix_dumpfil))
        except Exception as e:
            logw('Can\'t dump host-matrix to disk. Exception: {e}'.format(e=e))

if len(h_matrix) == 0:
    logw('There are no data in the host-matrix for further processing. Exit.')
    exit()

logw('Сreating an additional field in the host-matrix based on data from Vulners')
# формируем доп-поля в матрице на основе данных от вулнерса
current_host = 0
logw('Processed hosts')
for h in h_matrix:
    current_host += 1
    try:
        # список словарей из bulletitID + SCORE, для этого хоста
        h_bulletins = list()
        # список словарей из PKG + его SCORE + его bulletitID, для этого хоста
        h_packages_tmp = list()

        # из форматированного через jpath-джейсона получаем пакет, его бюллетень и балл
        for row in jpath.get_all(jpath=jpath_mask, data=h['vuln_data']):
            pkg = row['package']
            bull = row['bulletinID']
            score = row['cvss']['score']
            # добавляем double-словарь (bull,score) во временный список (для будущего шаманства с бюллетенями)
            h_bulletins.append({'name': bull, 'score': score})
            # добавляем triple-словарь (pkg,score,bull) во второй временный список (для будущего шаманства с пакетами)
            h_packages_tmp.append({'name': pkg, 'score': score, 'bull': bull})

        # убираем дубли одинаковые пакеты, но разные баллы; оставляя только самый высокий бал
        h_packages = list()
        # для каждого пакета в списке "пакет-балл"
        for r in h_packages_tmp:
            pkg = r['name']
            # оставляем во временном списке словарей (куцом) только сторки с пакетами = пакету текущей строки
            h_pkg_tmp = [i for i in h_packages_tmp if i['name'] == pkg]
            h_score_tmp = [0]
            # выбираем все баллы из списка словарей
            for s in h_pkg_tmp:
                h_score_tmp.append(s['score'])
            # определяем самый высокий балл
            score_max = max(map(float, h_score_tmp))
            # оставляем в матрице только строки с самым высоким баллом
            h_pkg = [i for i in h_pkg_tmp if i['score'] == score_max]
            h_packages.append(h_pkg[0])

        # фиксируем в матрице только уникальные записи
        h.update({'h_fix': h['vuln_data']['data']['cumulativeFix'].replace(',', ''),
                  'h_score': h['vuln_data']['data']['cvss']['score'],
                  'h_packages': uniq_list(h_packages),
                  'h_bulletins': uniq_list(h_bulletins)})

        logw('.', 0)
    except Exception as e:
        logw('[{current_host} of {total_hosts}] Skipping {v_name}. Exception: {e}'
             .format(current_host=current_host, total_hosts=total_hosts, v_name=h["v_name"], e=e))
        continue
logw(' total: {current_host}.'.format(current_host=current_host), 0)

f = open(c.zsender_data_file, 'w')
f_lld = open(c.zsender_lld_file, 'w')

logw('Сreating an LLD-data: CVSS-Scores and Cumulative-Fix commands')
current_host = 0
discovery_hosts = list()
for h in h_matrix:
    current_host += 1
    # формируем LLD-JSON
    try:
        discovery_hosts.append({'{#H.VNAME}': h['v_name'],
                                '{#H.HOST}': h['host_name'],
                                '{#H.ID}': h['hostid'],
                                '{#H.FIX}': h['h_fix'],
                                '{#H.SCORE}': h['h_score']})

        f.write('\"{zbx_h_hosts}\" vulners.hosts[{hostid}] {h_score}\n'
                .format(zbx_h_hosts=c.zbx_h_hosts, hostid=h["hostid"], h_score=h["h_score"]))
    except Exception as e:
        logw('[{current_host} of {total_hosts}] {v_name}. Exception: {e}'
             .format(current_host=current_host, total_hosts=total_hosts, v_name=h["v_name"], e=e))
        continue

# преобразовываем список в однострочный json без пробелов и пишем в файл
discovery_hosts_json = (json.dumps({'data': discovery_hosts})).replace(': ', ':').replace(', ', ',')

f_lld.write('\"{zbx_h_hosts}\" vulners.hosts_lld {discovery_hosts_json}\n'
            .format(zbx_h_hosts=c.zbx_h_hosts, discovery_hosts_json=discovery_hosts_json))

###########################
# ФОРМИРУЕМ МАТРИЦУ ПАКЕТОВ
###########################
logw('Creating an package-matrix.')

# цель - найти все хосты, на которых зааффектило этот пакет, для этого
pkg_matrix_tmp = list()
# для каждой строки в матрице
row_iter = 0
p_row_iter = 0
h_row_iter = 0
pp_iter = 0
for row in h_matrix:
    row_iter += 1
    try:
        # для каждого пакета в списке пакет-балл (из строки выше), делаем следующее
        for p_row in row['h_packages']:
            p_row_iter += 1
            # для каждой строки в матрице
            host_list = list()
            for h_row in h_matrix:
                h_row_iter += 1
                # для каждого пакета в списке пакет-бюллетени, из строки выше (по хостам)
                # проверяем что этот пакет, на этом хосте, соответсует тому, верхнеуровневому (уровень 2)
                for pp in h_row['h_packages']:
                    pp_iter += 1
                    if p_row['name'] == pp['name']:
                        # и если соответсвует - добавляем имя хоста к атрибутам пакета.
                        host_list.append(h_row['v_name'])
            pkg_matrix_tmp.append(
                {'pkg': p_row['name'], 'score': p_row['score'], 'bull': p_row['bull'], 'host_list': host_list})
        logw('.', 0)
    except Exception as e:
        logw('Skipping {v_name}. Exception: {e}'.format(v_name=row["v_name"], e=e))
        continue
logw(' total: {row_iter}.'.format(row_iter=row_iter), 0)
pkg_matrix = uniq_list(pkg_matrix_tmp)

# logw(f'package-matrix ROW: {row_iter}')
# logw(f'package-matrix ROW->PKG: {p_row_iter}')
# logw(f'package-matrixв ROW->PKG->HOST: {h_row_iter}')
# logw(f'package-matrix ROW->PKG->HOST->PKGS: {pp_iter}')

# формируем пакет LLD-данных
logw('Сreating an LLD-data for package monitoring.')

discovery_pkg = list()
# для каждого бюллетеня (строки) в матрице бюллетеней строим LLD-json c кол-вом хостов, именем, баллами, влиянием
for p in pkg_matrix:
    affected_h_cnt = len(p['host_list'])
    pkg = p['pkg']
    bull = p['bull']
    pkg_score = p['score']

    # пишем данные касательно кол-ва хостов, затронутых этим пакетом
    f.write('\"{zbx_h_pkgs}\" \"vulners.pkg[{pkg}]\" {affected_h_cnt}\n'
            .format(zbx_h_pkgs=c.zbx_h_pkgs, pkg=pkg, affected_h_cnt=affected_h_cnt))

    # формируем LLD-JSON
    discovery_pkg.append({'{#PKG.ID}': pkg,
                          '{#PKG.URL}': bull,
                          '{#PKG.SCORE}': pkg_score,
                          '{#PKG.AFFECTED}': affected_h_cnt,
                          '{#PKG.IMPACT}': int(affected_h_cnt * pkg_score),
                          '{#PKG.HOSTS}': '\n'.join(p['host_list'])})

# преобразовываем в однострочный json без пробелов и пишем в файл
discovery_pkg_json = (json.dumps({'data': discovery_pkg})).replace(': ', ':').replace(', ', ',')

f_lld.write('\"{zbx_h_pkgs}\" vulners.packages_lld {discovery_pkg_json}\n'
            .format(zbx_h_pkgs=c.zbx_h_pkgs, discovery_pkg_json=discovery_pkg_json))

##############################
# ФОРМИРУЕМ МАТРИЦУ БЮЛЛЕТЕНЕЙ
##############################
logw('Creating an bulletin-matrix.')

bulletins_d = dict()
full_bulletins_lst = list()
d_iter = 0
bull_iter = 0
hh_iter = 0
# из каждой строки в матрице хостов "ХОСТ-[СПИСОК_БЮЛЛЕТЕНЕЙ]" (уровень 1)
for d in h_matrix:
    d_iter += 1
    try:
        # обрабатыватываем поочередно каждый бюллетень из [СПИСОК_БЮЛЛЕТЕНЕЙ] (уровень 2)
        # todo: может быть стоит проверить что на хосте есть бюллетени, а не безусловно счтать что они есть?
        for bull in d['h_bulletins']:
            bull_iter += 1
            hh_list = list()
            # и теперь ищем этот бюллетень во всей _матрице_ хостов "ХОСТ-[СПИСОК_БЮЛЛЕТЕНЕЙ]" (уровень 3)
            for hh in h_matrix:
                hh_iter += 1
                # если встречается хоть раз - имя хоста, у которого бюллетень встречается, заносим во временный список
                cnt = hh['h_bulletins'].count(bull)
                if cnt > 0:
                    hh_list.append(hh['v_name'])

            # формируем словарь "БЮЛЛЕТЕНЬ-[СПИСОК_ХОСТОВ]" (обратный от уровня 1) и общий список
            bulletins_d = {'bull': bull, 'hosts': hh_list}
            # формируем список словарей (сырую матрицу) "БЮЛЛЕТЕНЬ-[СПИСОК_ХОСТОВ]" (обратную от host_matrix)
            full_bulletins_lst.append(bulletins_d)
        logw('.', 0)
    except Exception as e:
        logw('Skipping entrie {bull_iter}. Exception: {e}'.format(bull_iter=bull_iter, e=e))
        continue
logw(' total: {d_iter}.'.format(d_iter=d_iter), 0)
# logw(f'bulletin matirx ROW: {d_iter}')
# logw(f'bulletin matirx ROW->BULL: {bull_iter}')
# logw(f'bulletin matirx ROW->BULL->HOST: {hh_iter}')

# оставляем только уникальные записи в списке (итоговая матрица)
b_matrix = uniq_list(full_bulletins_lst)

# формируем пакет LLD-данных
logw('Сreating an LLD-data for bulletin monitoring..')

discovery_data = list()
# для каждого бюллетеня (строки) в матрице бюллетеней строим LLD-json c кол-вом хостов, именем, баллами, влиянием
for b in b_matrix:
    affected_h_cnt = len(b['hosts'])
    bullentin_name = b['bull']['name']
    bulletin_score = b['bull']['score']
    bulletin_impact = int(affected_h_cnt * bulletin_score)

    # пишем данные касательно кол-ва хостов, затронутых этим бюллетенем
    f.write('\"{zbx_h_bulls}\" vulners.bulletin[{bullentin_name}] {affected_h_cnt}\n'
            .format(zbx_h_bulls=c.zbx_h_bulls, bullentin_name=bullentin_name, affected_h_cnt=affected_h_cnt))

    # формируем LLD-JSON
    discovery_data.append({'{#BULLETIN.ID}': bullentin_name,
                           '{#BULLETIN.SCORE}': bulletin_score,
                           '{#BULLETIN.AFFECTED}': affected_h_cnt,
                           '{#BULLETIN.IMPACT}': bulletin_impact,
                           '{#BULLETIN.HOSTS}': '\n'.join(b['hosts'])})

# преобразовываем в однострочный json без пробелов и пишем в файл
discovery_json = (json.dumps({'data': discovery_data})).replace(': ', ':').replace(', ', ',')

f_lld.write('\"{zbx_h_bulls}\" vulners.bulletins_lld {discovery_json}\n'
            .format(zbx_h_bulls=c.zbx_h_bulls, discovery_json=discovery_json))

logw('Сreating an CVSS Score-based host-lists.')
score_list = list()

host_count_table = dict((score_value, 0) for score_value in range(0, 11))
for h in h_matrix:
    score_list.append(h['h_score'])
    score = float(h['h_score'])
    host_count_table[int(score)] += 1

# если вдруг список с баллами хостов пуст, пишем в него '0'
if len(score_list) == 0:
    score_list = [0]

logw('Сreating an aggregated data.')
# считаем аггрегированыне метрики и пишем их в файл
agg_score_median = median(map(float, score_list))
agg_score_mean = mean(map(float, score_list))
agg_score_max = max(map(float, score_list))
agg_score_min = min(map(float, score_list))

for intScore in host_count_table:
    f.write('\"{zbx_h_stats}\" vulners.hostsCountScore{intScore} {aff}\n'
            .format(zbx_h_stats=c.zbx_h_stats, intScore=intScore, aff=host_count_table.get(intScore)))
f.write('\"{zbx_h_stats}\" vulners.hostsCount {total_hosts}\n'
        .format(zbx_h_stats=c.zbx_h_stats, total_hosts=total_hosts))
f.write('\"{zbx_h_stats}\" vulners.scoreMedian {agg_score_median}\n'
        .format(zbx_h_stats=c.zbx_h_stats, agg_score_median=agg_score_median))
f.write('\"{zbx_h_stats}\" vulners.scoreMean {agg_score_mean}\n'
        .format(zbx_h_stats=c.zbx_h_stats, agg_score_mean=agg_score_mean))
f.write('\"{zbx_h_stats}\" vulners.scoreMax {agg_score_max}\n'
        .format(zbx_h_stats=c.zbx_h_stats, agg_score_max=agg_score_max))
f.write('\"{zbx_h_stats}\" vulners.scoreMin {agg_score_min}\n'
        .format(zbx_h_stats=c.zbx_h_stats, agg_score_min=agg_score_min))

f.close()
f_lld.close()

# пушим в заббикс полученные баллы и фиксы для всех хостов
push_cmd = 'zabbix_sender -z {zbx_server} -p {zbx_port} -i {zsender_data_file}'\
           .format(zbx_server=c.zbx_server, zbx_port=c.zbx_port, zsender_data_file=c.zsender_data_file)
push_lld_cmd = 'zabbix_sender -z {zbx_server} -p {zbx_port} -i {zsender_lld_file}'\
               .format(zbx_server=c.zbx_server, zbx_port=c.zbx_port, zsender_lld_file=c.zsender_lld_file)

if args.BypassZbxPush:
    logw('The transfer of data to zabbix is disabled, but can be performed by commands:')
    logw('{push_lld_cmd}; sleep 300; {push_cmd}'.format(push_lld_cmd=push_lld_cmd, push_cmd=push_cmd))
else:
    logw('Pushing data to Zabbix.')
    shell(push_lld_cmd)
    logw(push_lld_cmd)

    # чтобы LLD-метрики в Zabbix успели создаться нужен небольшой таймаут
    logw('sleep for 5 min')
    sleep(300)

    shell(push_cmd)
    logw(push_cmd)

logw('Work completed successfully.')
