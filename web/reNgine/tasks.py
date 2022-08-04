import csv
import json
import os
import random
import subprocess
from datetime import datetime
from this import d
from time import sleep
from unittest.mock import DEFAULT

import metafinder.extractor as metadata_extractor
import validators
import whatportis
import yaml
from degoogle import degoogle
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from dotted_dict import DottedDict
from emailfinder.extractor import (get_emails_from_baidu, get_emails_from_bing,
                                   get_emails_from_google)
from reNgine.celery import app
from reNgine.definitions import *
from scanEngine.models import EngineType
from selenium import webdriver
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from startScan.models import *
from startScan.models import Endpoint
from targetApp.models import Domain

from .common_func import *

'''
	All the background tasks to be executed in celery will be here
'''

@app.task
def run_system_commands(system_command):
	'''
		This function will run system commands in celery container
	'''
	os.system(system_command)


@app.task
def initiate_subtask(
		subdomain_id,
		scan_type,
		engine_id=None,
	):
	logger.info('Initiating Subtask')

	# TODO: OSINT IS NOT Currently SUPPORTED!, make it available in later releases
	if scan_type == 'osint':
		logger.warning('OSInt not supported yet. Skipping.')
		return
	
	# Get scan history and yaml Configuration for this subdomain
	subdomain = Subdomain.objects.get(id=subdomain_id)
	scan_history = ScanHistory.objects.get(id=subdomain.scan_history.id)

	# Create scan activity of SubScan Model
	current_scan_time = timezone.now()
	subscan = SubScan()
	subscan.start_scan_date = current_scan_time
	subscan.celery_id = initiate_subtask.request.id
	subscan.scan_history = scan_history
	subscan.subdomain = subdomain
	subscan.type = scan_type
	subscan.status = INITIATED_TASK
	subscan.save()

	if engine_id:
		engine = EngineType.objects.get(id=engine_id)
	else:
		engine = EngineType.objects.get(id=scan_history.scan_type.id)
	config = engine.yaml_configuration

	subscan.engine = engine
	subscan.save()

	results_dir = f'/usr/src/scan_results/{scan_history.results_dir}'
	timestr = datetime.strftime(timezone.now(), '%Y_%m_%d_%H_%M_%S')
	scan_name = f'{subdomain.name}_{timestr}'

	# if not results_dir exists, create one
	if not os.path.exists(results_dir):
		os.mkdir(results_dir)

	try:
		yaml_configuration = yaml.load(
			config,
			Loader=yaml.FullLoader)

		subscan.start_scan_date = current_scan_time
		subscan.status = RUNNING_TASK
		subscan.save()

		if scan_type == 'port_scan':
			filename = f'ports_{scan_name}.json'
			scan_history.port_scan = True
			scan_history.save()
			nabuu_scan(
				scan_history,
				0,
				yaml_configuration,
				results_dir,
				subdomain=subdomain.name,
				filename=filename,
				subscan=subscan
			)
		elif scan_type == 'dir_fuzz':
			filename = f'dir_fuzz_{scan_name}.json'
			scan_history.dir_file_fuzz = True
			scan_history.save()
			directory_fuzz(
				scan_history,
				0,
				yaml_configuration,
				results_dir,
				subdomain=subdomain.name,
				filename=filename,
				subscan=subscan
			)
		elif scan_type == 'endpoint':
			filename = f'endpoints_{scan_name}.txt'
			scan_history.fetch_url = True
			scan_history.save()
			fetch_endpoints(
				scan_history,
				0,
				yaml_configuration,
				results_dir,
				subdomain=subdomain,
				filename=filename,
				subscan=subscan
			)
		elif scan_type == 'vulnerability_scan':
			filename = f'vuln_{scan_name}.txt'
			scan_history.vulnerability_scan = True
			scan_history.save()
			vulnerability_scan(
				scan_history,
				0,
				yaml_configuration,
				results_dir,
				subdomain=subdomain,
				filename=filename,
				subscan=subscan
			)
		else:
			logger.error(f'Scan type "{scan_type}" is not supported yet.')

		task_status = SUCCESS_TASK

	except Exception as e:
		logger.error(e)
		task_status = FAILED_TASK
		subscan.error_message = str(e)
	finally:
		subscan.stop_scan_date = timezone.now()
		subscan.status = task_status
		subscan.save()


@app.task
def initiate_scan(
		domain_id,
		scan_history_id,
		scan_type,
		engine_type,
		imported_subdomains=None,
		out_of_scope_subdomains=[]):
	'''
	scan_type = 0 -> immediate scan, need not create scan object
	scan_type = 1 -> scheduled scan
	'''
	engine_object = EngineType.objects.get(pk=engine_type)
	domain = Domain.objects.get(pk=domain_id)
	if scan_type == 1:
		task = ScanHistory()
		task.scan_status = -1
	elif scan_type == 0:
		task = ScanHistory.objects.get(pk=scan_history_id)

	# save the last scan date for domain model
	domain.last_scan_date = timezone.now()
	domain.save()

	# once the celery task starts, change the task status to Started
	task.scan_type = engine_object
	task.celery_id = initiate_scan.request.id
	task.domain = domain
	task.scan_status = 1
	task.start_scan_date = timezone.now()
	task.subdomain_discovery = True if engine_object.subdomain_discovery else False
	task.waf_detection = True if engine_object.waf_detection else False
	task.dir_file_fuzz = True if engine_object.dir_file_fuzz else False
	task.port_scan = True if engine_object.port_scan else False
	task.fetch_url = True if engine_object.fetch_url else False
	task.osint = True if engine_object.osint else False
	task.screenshot = True if engine_object.screenshot else False
	task.vulnerability_scan = True if engine_object.vulnerability_scan else False
	task.save()

	activity_id = create_scan_activity(task, "Scanning Started", 2)
	results_dir = '/usr/src/scan_results/'
	os.chdir(results_dir)

	notification = Notification.objects.all()

	if notification and notification[0].send_scan_status_notif:
		send_notification('reNgine has initiated recon for target {} with engine type {}'.format(domain.name, engine_object.engine_name))

	timestr = datetime.strftime(timezone.now(), '%Y_%m_%d_%H_%M_%S')
	current_scan_dir = domain.name + '_' + timestr
	try:
		os.mkdir(current_scan_dir)
		task.results_dir = current_scan_dir
		task.save()
	except Exception as exception:
		logger.error(exception)
		scan_failed(task)

	yaml_configuration = None
	try:
		yaml_configuration = yaml.load(
			task.scan_type.yaml_configuration,
			Loader=yaml.FullLoader)
	except Exception as exception:
		logger.error(exception)
		# TODO: Put failed reason on db

	'''
	Add GF patterns name to db for dynamic URLs menu
	'''
	if engine_object.fetch_url and GF_PATTERNS in yaml_configuration[FETCH_URL]:
		task.used_gf_patterns = ','.join(
			pattern for pattern in yaml_configuration[FETCH_URL][GF_PATTERNS])
		task.save()

	results_dir = results_dir + current_scan_dir

	# put all imported subdomains into txt file and also in Subdomain model
	if imported_subdomains:
		extract_imported_subdomain(
			imported_subdomains, task, domain, results_dir)

	if not yaml_configuration:
		return
	'''
	a target in itself is a subdomain, some tool give subdomains as
	www.yogeshojha.com but url and everything else resolves to yogeshojha.com
	In that case, we would already need to store target itself as subdomain
	'''
	initial_subdomain_file = '/target_domain.txt' if task.subdomain_discovery else '/sorted_subdomain_collection.txt'

	subdomain_file = open(results_dir + initial_subdomain_file, "w")
	subdomain_file.write(domain.name + "\n")
	subdomain_file.close()

	if(task.subdomain_discovery):
		activity_id = create_scan_activity(task, "Subdomain Scanning", 1)
		try:
			subdomain_scan(
				task,
				domain,
				yaml_configuration,
				results_dir,
				activity_id,
				out_of_scope_subdomains
				)
		except Exception as e:
			logger.error(e)
			update_last_activity(activity_id, 0, error_message=str(e))

	else:
		skip_subdomain_scan(task, domain, results_dir)

	update_last_activity(activity_id, 2)
	activity_id = create_scan_activity(task, "HTTP Crawler", 1)
	http_crawler(
		task,
		domain,
		yaml_configuration,
		results_dir,
		activity_id)
	update_last_activity(activity_id, 2)

	# start wafw00f
	if(task.waf_detection):
		try:
			activity_id = create_scan_activity(task, "Detecting WAF", 1)
			check_waf(task, results_dir)
			update_last_activity(activity_id, 2)
		except Exception as e:
			logger.error(e)
			update_last_activity(activity_id, 0, error_message=str(e))

	try:
		if task.screenshot:
			activity_id = create_scan_activity(
				task, "Visual Recon - Screenshot", 1)
			grab_screenshot(
				task,
				domain,
				yaml_configuration,
				current_scan_dir,
				activity_id)
			update_last_activity(activity_id, 2)
	except Exception as e:
		logger.error(e)
		update_last_activity(activity_id, 0, error_message=str(e))
		task.error_message = str(e)
		task.save()

	try:
		if(task.port_scan):
			activity_id = create_scan_activity(task, "Port Scanning", 1)
			nabuu_scan(task, activity_id, yaml_configuration, results_dir, domain)
			update_last_activity(activity_id, 2)
	except Exception as e:
		logger.error(e)
		update_last_activity(activity_id, 0, error_message=str(e))
		task.error_message = str(e)
		task.save()

	try:
		if task.osint:
			activity_id = create_scan_activity(task, "OSINT Running", 1)
			perform_osint(task, domain, yaml_configuration, results_dir)
			update_last_activity(activity_id, 2)
	except Exception as e:
		logger.error(e)
		update_last_activity(activity_id, 0, error_message=str(e))
		task.error_message = str(e)
		task.save()


	try:
		if task.dir_file_fuzz:
			activity_id = create_scan_activity(task, "Directory Search", 1)
			directory_fuzz(
				task,
				activity_id,
				yaml_configuration,
				results_dir,
				domain=domain,
			)
			update_last_activity(activity_id, 2)
	except Exception as e:
		logger.error(e)
		update_last_activity(activity_id, 0, error_message=str(e))
		task.error_message = str(e)
		task.save()

	try:
		if task.fetch_url:
			activity_id = create_scan_activity(task, "Fetching endpoints", 1)
			fetch_endpoints(
				task,
				activity_id,
				yaml_configuration,
				results_dir,
				domain=domain,
				)
			update_last_activity(activity_id, 2)
	except Exception as e:
		logger.error(e)
		update_last_activity(activity_id, 0, error_message=str(e))
		task.error_message = str(e)
		task.save()

	try:
		if task.vulnerability_scan:
			activity_id = create_scan_activity(task, "Vulnerability Scan", 1)
			vulnerability_scan(
				task,
				activity_id,
				yaml_configuration,
				results_dir,
				domain=domain,
			)
			update_last_activity(activity_id, 2)
	except Exception as e:
		logger.error(e)
		update_last_activity(activity_id, 0, error_message=str(e))
		task.error_message = str(e)
		task.save()

	activity_id = create_scan_activity(task, "Scan Completed", 2)
	if notification and notification[0].send_scan_status_notif:
		send_notification('*Scan Completed*\nreNgine has finished performing recon on target {}.'.format(domain.name))

	'''
	Once the scan is completed, save the status to successful
	'''
	if ScanActivity.objects.filter(scan_of=task).filter(status=0).all():
		task.scan_status = 0
	else:
		task.scan_status = 2
	task.stop_scan_date = timezone.now()
	task.save()
	# cleanup results
	# delete_scan_data(results_dir)
	return {"status": True}


def skip_subdomain_scan(task, domain, results_dir):
	# store default target as subdomain
	'''
	If the imported subdomain already has target domain saved, we can skip this
	'''
	if not Subdomain.objects.filter(
			scan_history=task,
			name=domain.name).exists():

		subdomain_dict = DottedDict({
			'name': domain.name,
			'scan_history': task,
			'target_domain': domain
		})
		save_subdomain(subdomain_dict)

	# Save target into target_domain.txt
	with open('{}/target_domain.txt'.format(results_dir), 'w+') as file:
		file.write(domain.name + '\n')

	file.close()

	'''
	We can have two conditions, either subdomain scan happens, or subdomain scan
	does not happen, in either cases, because we are using import subdomain, we
	need to collect and sort all the subdomains

	Write target domain into subdomain_collection
	'''

	os.system(
		'cat {0}/target_domain.txt > {0}/subdomain_collection.txt'.format(results_dir))

	os.system(
		'cat {0}/from_imported.txt > {0}/subdomain_collection.txt'.format(results_dir))

	os.system('rm -f {}/from_imported.txt'.format(results_dir))

	'''
	Sort all Subdomains
	'''
	os.system(
		'sort -u {0}/subdomain_collection.txt -o {0}/sorted_subdomain_collection.txt'.format(results_dir))

	os.system('rm -f {}/subdomain_collection.txt'.format(results_dir))


def extract_imported_subdomain(imported_subdomains, task, domain, results_dir):
	valid_imported_subdomains = [subdomain for subdomain in imported_subdomains if validators.domain(
		subdomain) and domain.name == get_domain_from_subdomain(subdomain)]

	# remove any duplicate
	valid_imported_subdomains = list(set(valid_imported_subdomains))

	with open('{}/from_imported.txt'.format(results_dir), 'w+') as file:
		for subdomain_name in valid_imported_subdomains:
			# save _subdomain to Subdomain model db
			if not Subdomain.objects.filter(
					scan_history=task, name=subdomain_name).exists():

				subdomain_dict = DottedDict({
					'scan_history': task,
					'target_domain': domain,
					'name': subdomain_name,
					'is_imported_subdomain': True
				})
				save_subdomain(subdomain_dict)
				# save subdomain to file
				file.write('{}\n'.format(subdomain_name))

	file.close()


def subdomain_scan(
		task,
		domain,
		yaml_configuration,
		results_dir,
		activity_id,
		out_of_scope_subdomains=None,
		subscan=None
	):

	# get all external subdomain enum tools
	default_subdomain_tools = [tool.name.lower() for tool in InstalledExternalTool.objects.filter(is_default=True).filter(is_subdomain_gathering=True)]
	custom_subdomain_tools = [tool.name.lower() for tool in InstalledExternalTool.objects.filter(is_default=False).filter(is_subdomain_gathering=True)]

	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		send_notification('Subdomain Gathering for target {} has been started'.format(domain.name))

	subdomain_scan_results_file = results_dir + '/sorted_subdomain_collection.txt'

	# check for all the tools and add them into string
	# if tool selected is all then make string, no need for loop
	if ALL in yaml_configuration[SUBDOMAIN_DISCOVERY][USES_TOOLS]:
		tools = 'amass-active amass-passive assetfinder sublist3r subfinder oneforall '
		tools += custom_subdomain_tools
	else:
		tools = ' '.join(
			list(map(str.lower, yaml_configuration[SUBDOMAIN_DISCOVERY][USES_TOOLS])))

	logger.info(tools)
	logger.info(default_subdomain_tools)
	logger.info(custom_subdomain_tools)

	# check for THREADS, by default 10
	threads = yaml_configuration.get(SUBDOMAIN_DISCOVERY, {}).get(THREADS, 20)

	try:
		for tool in tools.split(' '):
			if tool in default_subdomain_tools:
				if tool == 'amass-passive':
					amass_command = 'amass enum -passive -d {} -o {}/from_amass.txt'.format(
							domain.name, results_dir)

					if USE_AMASS_CONFIG in yaml_configuration[SUBDOMAIN_DISCOVERY] and yaml_configuration[SUBDOMAIN_DISCOVERY][USE_AMASS_CONFIG]:
						amass_command += ' -config /root/.config/amass.ini'
					# Run Amass Passive
					logger.info(amass_command)
					process = subprocess.Popen(amass_command.split())
					process.wait()

				elif tool == 'amass-active':
					amass_command = 'amass enum -active -d {} -o {}/from_amass_active.txt'.format(
							domain.name, results_dir)

					if USE_AMASS_CONFIG in yaml_configuration[SUBDOMAIN_DISCOVERY] and yaml_configuration[SUBDOMAIN_DISCOVERY][USE_AMASS_CONFIG]:
						amass_command += ' -config /root/.config/amass.ini'

					if AMASS_WORDLIST in yaml_configuration[SUBDOMAIN_DISCOVERY]:
						wordlist = yaml_configuration[SUBDOMAIN_DISCOVERY][AMASS_WORDLIST]
						if wordlist == 'default':
							wordlist_path = '/usr/src/wordlist/deepmagic.com-prefixes-top50000.txt'
						else:
							wordlist_path = '/usr/src/wordlist/' + wordlist + '.txt'
							if not os.path.exists(wordlist_path):
								wordlist_path = '/usr/src/' + AMASS_WORDLIST
						amass_command = amass_command + \
							' -brute -w {}'.format(wordlist_path)

					# Run Amass Active
					logger.info(amass_command)
					process = subprocess.Popen(amass_command.split())
					process.wait()

				elif tool == 'assetfinder':
					assetfinder_command = 'assetfinder --subs-only {} > {}/from_assetfinder.txt'.format(
						domain.name, results_dir)

					# Run Assetfinder
					logger.info(assetfinder_command)
					process = subprocess.Popen(assetfinder_command.split())
					process.wait()

				elif tool == 'sublist3r':
					sublist3r_command = 'python3 /usr/src/github/Sublist3r/sublist3r.py -d {} -t {} -o {}/from_sublister.txt'.format(
						domain.name, threads, results_dir)

					# Run sublist3r
					logger.info(sublist3r_command)
					process = subprocess.Popen(sublist3r_command.split())
					process.wait()

				elif tool == 'subfinder':
					subfinder_command = 'subfinder -d {} -t {} -o {}/from_subfinder.txt'.format(
						domain.name, threads, results_dir)

					if USE_SUBFINDER_CONFIG in yaml_configuration[SUBDOMAIN_DISCOVERY] and yaml_configuration[SUBDOMAIN_DISCOVERY][USE_SUBFINDER_CONFIG]:
						subfinder_command += ' -config /root/.config/subfinder/config.yaml'

					# Run Subfinder
					logger.info(subfinder_command)
					process = subprocess.Popen(subfinder_command.split())
					process.wait()

				elif tool == 'oneforall':
					oneforall_command = 'python3 /usr/src/github/OneForAll/oneforall.py --target {} run'.format(
						domain.name, results_dir)

					# Run OneForAll
					logger.info(oneforall_command)
					process = subprocess.Popen(oneforall_command.split())
					process.wait()

					extract_subdomain = "cut -d',' -f6 /usr/src/github/OneForAll/results/{}.csv >> {}/from_oneforall.txt".format(
						domain.name, results_dir)

					os.system(extract_subdomain)

					# remove the results from oneforall directory
					os.system(
						'rm -rf /usr/src/github/OneForAll/results/{}.*'.format(domain.name))

			elif tool.lower() in custom_subdomain_tools:
				# this is for all the custom tools, and tools runs based on instalaltion steps provided
				if InstalledExternalTool.objects.filter(name__icontains=tool.lower()).exists():
					custom_tool = InstalledExternalTool.objects.get(name__icontains=tool)
					execution_command = custom_tool.subdomain_gathering_command
					print(execution_command)
					# replace syntax with actual commands and path
					if '{TARGET}' in execution_command and '{OUTPUT}' in execution_command:
						execution_command = execution_command.replace('{TARGET}', domain.name)
						execution_command = execution_command.replace('{OUTPUT}', '{}/from_{}.txt'.format(results_dir, tool))
						execution_command = execution_command.replace('{PATH}', custom_tool.github_clone_path) if '{PATH}' in execution_command else execution_command
						logger.info('Custom tool {} running with command {}'.format(tool, execution_command))
						process = subprocess.Popen(execution_command.split())
						process.wait()
					else:
						logger.error('Sorry can not run this tool! because TARGET and OUTPUT are not available!')
	except Exception as e:
		logger.error(e)

	'''
	All tools have gathered the list of subdomains with filename
	initials as from_*
	We will gather all the results in one single file, sort them and
	remove the older results from_*
	'''
	os.system(
		'cat {0}/*.txt > {0}/subdomain_collection.txt'.format(results_dir))

	'''
	Write target domain into subdomain_collection
	'''
	os.system(
		'cat {0}/target_domain.txt >> {0}/subdomain_collection.txt'.format(results_dir))

	'''
	Remove all the from_* files
	'''
	os.system('rm -f {}/from*'.format(results_dir))

	'''
	Sort all Subdomains
	'''
	os.system(
		'sort -u {0}/subdomain_collection.txt -o {0}/sorted_subdomain_collection.txt'.format(results_dir))

	os.system('rm -f {}/subdomain_collection.txt'.format(results_dir))

	'''
	The final results will be stored in sorted_subdomain_collection.
	'''
	# parse the subdomain list file and store in db
	with open(subdomain_scan_results_file) as subdomain_list:
		for _subdomain in subdomain_list:
			__subdomain = _subdomain.rstrip('\n')
			if not Subdomain.objects.filter(scan_history=task, name=__subdomain).exists(
			) and validators.domain(__subdomain) and __subdomain not in out_of_scope_subdomains:
				subdomain_dict = DottedDict({
					'scan_history': task,
					'target_domain': domain,
					'name': __subdomain,
				})
				save_subdomain(subdomain_dict)

	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		subdomains_count = Subdomain.objects.filter(scan_history=task).count()
		send_notification('Subdomain Gathering for target {} has been completed and has discovered *{}* subdomains.'.format(domain.name, subdomains_count))
	if notification and notification[0].send_scan_output_file:
		send_files_to_discord(results_dir + '/sorted_subdomain_collection.txt')

	# check for any subdomain changes and send notif if any
	if notification and notification[0].send_subdomain_changes_notif:
		newly_added_subdomain = get_new_added_subdomain(task.id, domain.id)
		if newly_added_subdomain:
			message = "**{} New Subdomains Discovered on domain {}**".format(newly_added_subdomain.count(), domain.name)
			for subdomain in newly_added_subdomain:
				message += "\n• {}".format(subdomain.name)
			send_notification(message)

		removed_subdomain = get_removed_subdomain(task.id, domain.id)
		if removed_subdomain:
			message = "**{} Subdomains are no longer available on domain {}**".format(removed_subdomain.count(), domain.name)
			for subdomain in removed_subdomain:
				message += "\n• {}".format(subdomain.name)
			send_notification(message)

	# check for interesting subdomains and send notif if any
	if notification and notification[0].send_interesting_notif:
		interesting_subdomain = get_interesting_subdomains(task.id, domain.id)
		print(interesting_subdomain)
		if interesting_subdomain:
			message = "**{} Interesting Subdomains Found on domain {}**".format(interesting_subdomain.count(), domain.name)
			for subdomain in interesting_subdomain:
				message += "\n• {}".format(subdomain.name)
			send_notification(message)


def get_new_added_subdomain(scan_id, domain_id):
	scan_history = ScanHistory.objects.filter(
		domain=domain_id).filter(
			subdomain_discovery=True).filter(
				id__lte=scan_id)
	if scan_history.count() > 1:
		last_scan = scan_history.order_by('-start_scan_date')[1]
		scanned_host_q1 = Subdomain.objects.filter(
			scan_history__id=scan_id).values('name')
		scanned_host_q2 = Subdomain.objects.filter(
			scan_history__id=last_scan.id).values('name')
		added_subdomain = scanned_host_q1.difference(scanned_host_q2)

		return Subdomain.objects.filter(
			scan_history=scan_id).filter(
				name__in=added_subdomain)

def get_removed_subdomain(scan_id, domain_id):
	scan_history = ScanHistory.objects.filter(
		domain=domain_id).filter(
			subdomain_discovery=True).filter(
				id__lte=scan_id)
	if scan_history.count() > 1:
		last_scan = scan_history.order_by('-start_scan_date')[1]
		scanned_host_q1 = Subdomain.objects.filter(
			scan_history__id=scan_id).values('name')
		scanned_host_q2 = Subdomain.objects.filter(
			scan_history__id=last_scan.id).values('name')
		removed_subdomains = scanned_host_q2.difference(scanned_host_q1)

		return Subdomain.objects.filter(
			scan_history=last_scan).filter(
				name__in=removed_subdomains)


def http_crawler(task, domain, yaml_configuration, results_dir, activity_id, threads=100):
	'''
	This function is runs right after subdomain gathering, and gathers important
	like page title, http status, etc
	HTTP Crawler runs by default
	'''
	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		send_notification('HTTP Crawler for target {} has been initiated.'.format(domain.name))

	alive_file_location = results_dir + '/alive.txt'
	httpx_results_file = results_dir + '/httpx.json'

	subdomain_scan_results_file = results_dir + '/sorted_subdomain_collection.txt'
	httpx_command = '/go/bin/httpx -status-code -content-length -title -tech-detect -cdn -ip -follow-host-redirects -random-agent -t {}'.format(threads)

	proxy = get_random_proxy()

	if proxy:
		httpx_command += " --http-proxy {} ".format(proxy)

	if CUSTOM_HEADER in yaml_configuration and yaml_configuration[CUSTOM_HEADER]:
		httpx_command += ' -H "{}" '.format(yaml_configuration[CUSTOM_HEADER])

	httpx_command += ' -json -o {} -l {}'.format(
		httpx_results_file,
		subdomain_scan_results_file
	)
	logger.info(httpx_command)
	os.system(remove_cmd_injection_chars(httpx_command))

	# alive subdomains from httpx
	alive_file = open(alive_file_location, 'w')

	# writing httpx results
	if os.path.isfile(httpx_results_file):
		httpx_json_result = open(httpx_results_file, 'r')
		lines = httpx_json_result.readlines()
		for line in lines:
			json_st = json.loads(line.strip())
			try:
				# fallback for older versions of httpx
				if 'url' in json_st:
					subdomain = Subdomain.objects.get(
					scan_history=task, name=json_st['input'])
				else:
					subdomain = Subdomain.objects.get(
						scan_history=task, name=json_st['url'].split("//")[-1])
				'''
				Saving Default http urls to EndPoint
				'''
				endpoint = EndPoint()
				endpoint.scan_history = task
				endpoint.target_domain = domain
				endpoint.subdomain = subdomain
				if 'url' in json_st:
					endpoint.http_url = json_st['url']
					subdomain.http_url = json_st['url']
				if 'status-code' in json_st:
					endpoint.http_status = json_st['status-code']
					subdomain.http_status = json_st['status-code']
				if 'title' in json_st:
					endpoint.page_title = json_st['title']
					subdomain.page_title = json_st['title']
				if 'content-length' in json_st:
					endpoint.content_length = json_st['content-length']
					subdomain.content_length = json_st['content-length']
				if 'content-type' in json_st:
					endpoint.content_type = json_st['content-type']
					subdomain.content_type = json_st['content-type']
				if 'webserver' in json_st:
					endpoint.webserver = json_st['webserver']
					subdomain.webserver = json_st['webserver']
				if 'response-time' in json_st:
					response_time = float(
						''.join(
							ch for ch in json_st['response-time'] if not ch.isalpha()))
					if json_st['response-time'][-2:] == 'ms':
						response_time = response_time / 1000
					endpoint.response_time = response_time
					subdomain.response_time = response_time
				if 'cnames' in json_st:
					cname_list = ','.join(json_st['cnames'])
					subdomain.cname = cname_list
				discovered_date = timezone.now()
				endpoint.discovered_date = discovered_date
				subdomain.discovered_date = discovered_date
				endpoint.is_default = True
				endpoint.save()
				subdomain.save()
				if 'technologies' in json_st:
					for _tech in json_st['technologies']:
						if Technology.objects.filter(name=_tech).exists():
							tech = Technology.objects.get(name=_tech)
						else:
							tech = Technology(name=_tech)
							tech.save()
						subdomain.technologies.add(tech)
						endpoint.technologies.add(tech)
				if 'a' in json_st:
					for _ip in json_st['a']:
						if IpAddress.objects.filter(address=_ip).exists():
							ip = IpAddress.objects.get(address=_ip)
						else:
							ip = IpAddress(address=_ip)
							if 'cdn' in json_st:
								ip.is_cdn = json_st['cdn']
						# add geo iso
						subprocess_output = subprocess.getoutput(['geoiplookup {}'.format(_ip)])
						if 'IP Address not found' not in subprocess_output and "can't resolve hostname" not in subprocess_output:
							country_iso = subprocess_output.split(':')[1].strip().split(',')[0]
							country_name = subprocess_output.split(':')[1].strip().split(',')[1].strip()
							iso_object, _ = CountryISO.objects.get_or_create(
								iso=country_iso,
								name=country_name
							)
							ip.geo_iso = iso_object
						ip.save()
						subdomain.ip_addresses.add(ip)
				if 'host' in json_st:
					_ip = json_st['host']
					if IpAddress.objects.filter(address=_ip).exists():
						ip = IpAddress.objects.get(address=_ip)
					else:
						ip = IpAddress(address=_ip)
						if 'cdn' in json_st:
							ip.is_cdn = json_st['cdn']
					# add geo iso
					subprocess_output = subprocess.getoutput(['geoiplookup {}'.format(_ip)])
					if 'IP Address not found' not in subprocess_output and "can't resolve hostname" not in subprocess_output:
						country_iso = subprocess_output.split(':')[1].strip().split(',')[0]
						country_name = subprocess_output.split(':')[1].strip().split(',')[1].strip()
						iso_object, _ = CountryISO.objects.get_or_create(
							iso=country_iso,
							name=country_name
						)
						ip.geo_iso = iso_object
					ip.save()
				if json_st.get('status-code') < 400:
					alive_file.write(json_st['url'] + '\n')
				subdomain.save()
				endpoint.save()
			except Exception as exception:
				logger.error(exception)
	alive_file.close()

	if notification and notification[0].send_scan_status_notif:
		alive_count = Subdomain.objects.filter(
			scan_history__id=task.id).values('name').distinct().filter(
			http_status__exact=200).count()
		send_notification('HTTP Crawler for target {} has been completed.\n\n {} subdomains were alive (http status 200).'.format(domain.name, alive_count))


def grab_screenshot(task, domain, yaml_configuration, results_dir, activity_id):
	'''
	This function is responsible for taking screenshots
	'''
	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		send_notification('reNgine is currently gathering screenshots for {}'.format(domain.name))

	output_screenshots_path = f'{results_dir}/screenshots'
	result_csv_path = f'{results_dir}/screenshots/Requests.csv'
	alive_subdomains_path = f'{results_dir}/alive.txt'
	eyewitness_command = 'python3 /usr/src/github/EyeWitness/Python/EyeWitness.py'
	eyewitness_command += f' -f {alive_subdomains_path} -d {output_screenshots_path} --no-prompt '

	timeout = yaml_configuration.get(SCREENSHOT, {}).get(TIMEOUT, 0)
	if timeout > 0:
		eyewitness_command += f' --timeout {timeout}'

	threads = yaml_configuration.get(SCREENSHOT, {}).get(THREADS, 0)
	if threads > 0:
		eyewitness_command += f' --threads {threads}'

	logger.info(eyewitness_command)
	process = subprocess.Popen(eyewitness_command.split())
	process.wait()

	if os.path.isfile(result_csv_path):
		logger.info('Gathering Eyewitness results')
		with open(result_csv_path, 'r') as file:
			reader = csv.reader(file)
			for row in reader:
				if row[3] == 'Successful' \
					and Subdomain.objects.filter(
						scan_history__id=task.id).filter(name=row[2]).exists():
					subdomain = Subdomain.objects.get(
						scan_history__id=task.id,
						name=row[2]
					)
					subdomain.screenshot_path = row[4].replace(
						'/usr/src/scan_results/',
						''
					)
					subdomain.save()

	# remove all db, html extra files in screenshot results
	os.system('rm -rf {0}/*.csv {0}/*.db {0}/*.js {0}/*.html {0}/*.css'.format(
		output_screenshots_path,
	))
	os.system('rm -rf {0}/source'.format(
		output_screenshots_path,
	))

	if notification and notification[0].send_scan_status_notif:
		send_notification('reNgine has finished gathering screenshots for {}'.format(domain.name))

def nabuu_scan(
		scan_history,
		activity_id,
		yaml_configuration,
		results_dir,
		domain=None,
		subdomain=None,
		filename='ports.json',
		subscan=None
	):
	# Random sleep to prevent ip and port being overwritten
	sleep(random.randint(1,5))
	'''
	This function is responsible for running the port scan
	'''

	domain_name = domain.name if domain else subdomain
	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		send_notification('Port Scan initiated for {}'.format(domain_name))

	cmd = 'naabu'
	if domain:
		subdomains_path = f'{results_dir}/sorted_subdomain_collection.txt'
		cmd += f' -list {subdomains_path}'
	elif subdomain:
		cmd += f' -host {subdomain}'
		
	cmd += ' -exclude-cdn '

	# check the yaml_configuration and choose the ports to be scanned
	scan_ports = '-'  # default port scan everything
	if PORTS in yaml_configuration[PORT_SCAN]:
		# TODO:  legacy code, remove top-100 in future versions
		all_ports = yaml_configuration[PORT_SCAN][PORTS]
		if 'full' in all_ports:
			cmd += ' -p -'
		elif 'top-100' in all_ports:
			cmd += ' -top-ports 100 '
		elif 'top-1000' in all_ports:
			cmd += ' -top-ports 1000 '
		else:
			scan_ports = ','.join(
				str(port) for port in all_ports)
			cmd += ' -p {} '.format(scan_ports)

	# check for exclude ports
	if EXCLUDE_PORTS in yaml_configuration[PORT_SCAN] and yaml_configuration[PORT_SCAN][EXCLUDE_PORTS]:
		exclude_ports = ','.join(
			str(port) for port in yaml_configuration['port_scan']['exclude_ports'])
		cmd = cmd + \
			' -exclude-ports {} '.format(exclude_ports)

	if NAABU_RATE in yaml_configuration[PORT_SCAN] and yaml_configuration[PORT_SCAN][NAABU_RATE] > 0:
		cmd = cmd + \
			' -rate {} '.format(
				yaml_configuration[PORT_SCAN][NAABU_RATE])
			#new format for naabu config
	if USE_NAABU_CONFIG in yaml_configuration[PORT_SCAN] and yaml_configuration[PORT_SCAN][USE_NAABU_CONFIG]:
		cmd += ' -config /root/.config/naabu/config.yaml '

	# Output JSON
	output_filepath = f'{results_dir}/{filename}'
	cmd += f' -json -o {output_filepath}'

	# run naabu
	logger.info(cmd)
	process = subprocess.Popen(cmd.split())
	process.wait()

	# writing port results
	try:
		port_json_result = open(output_filepath, 'r')
		lines = port_json_result.readlines()
		for line in lines:
			json_st = json.loads(line.strip())
			port_number = json_st['port']
			ip_address = json_st['ip']
			host = json_st['host']

			# Add port to DB
			port_query = Port.objects.filter(number__exact=port_number)
			if port_query.exists():
				port = Port.objects.get(number=port_number)
			else:
				port = Port()
				port.number = port_number
				port.is_uncommon = port_number in UNCOMMON_WEB_PORTS
				port_details = whatportis.get_ports(str(port_number))
				if len(port_details) > 0:
					port.service_name = port_details[0].name
					port.description = port_details[0].description
				port.save()

			# Add IP DB
			ip_query = IpAddress.objects.filter(address=ip_address)
			if ip_query.exists():
				ip = ip_query.first()
			else:
				ip = IpAddress()
				ip.address = ip_address
				ip.save()
			ip.ports.add(port)
			ip.save()
			if subscan:
				ip.ip_subscan_ids.add(subscan)
				ip.save()

			# Add IP to Subdomain in DB
			subdomain_query = Subdomain.objects.filter(name=host, scan_history=scan_history, ip_addresses__address=ip_address)
			if subdomain_query.exists():
				subdomain = subdomain_query.first()
				subdomain.ip_addresses.add(ip)
				subdomain.save()

	except BaseException as exception:
		logger.error(exception)
		if not subscan:
			update_last_activity(activity_id, 0)
		raise Exception(exception)

	if notification and notification[0].send_scan_status_notif:
		port_count = Port.objects.filter(
			ports__in=IpAddress.objects.filter(
				ip_addresses__in=Subdomain.objects.filter(
					scan_history__id=scan_history.id))).distinct().count()
		send_notification('reNgine has finished Port Scanning on {} and has identified {} ports.'.format(domain_name, port_count))

	if notification and notification[0].send_scan_output_file:
		send_files_to_discord(results_dir + '/ports.json')


def check_waf(scan_history, results_dir):
	'''
	This function will check for the WAF being used in subdomains using wafw00f
	and this is done using passing alive.txt to the wafw00f
	Check if alive.txt exits, chances are that during the http crawling, none of
	the subdomains are alive, http_200
	'''
	alive_file = results_dir + '/alive.txt'
	output_filename = results_dir + '/wafw00f.txt'
	if os.path.isfile(alive_file):
		wafw00f_command = 'wafw00f -i {} -o {}'.format(
			alive_file,
			output_filename
		)

		logger.info(wafw00f_command)

		process = subprocess.Popen(wafw00f_command.split())
		process.wait()

		# check if wafw00f has generated output file
		if os.path.isfile(output_filename):
			with open(output_filename) as file:
				lines = file.readlines()
				for line in lines:
					# split by 3 space!
					splitted = line.split('   ')
					# remove all empty strings
					strs = [string for string in splitted if string]
					# 0th pos is url and 1st pos is waf, remove /n from waf
					waf = strs[1].strip()
					waf_name = waf[:waf.find('(')].strip()
					waf_manufacturer = waf[waf.find('(')+1:waf.find(')')].strip()
					http_url = strs[0].strip()
					if waf_name != 'None':
						if Waf.objects.filter(
							name=waf_name,
							manufacturer=waf_manufacturer
							).exists():
							waf_obj = Waf.objects.get(
								name=waf_name,
								manufacturer=waf_manufacturer
							)
						else:
							waf_obj = Waf(
								name=waf_name,
								manufacturer=waf_manufacturer
							)
							waf_obj.save()

						if Subdomain.objects.filter(
							scan_history=scan_history,
							http_url=http_url
							).exists():

							subdomain = Subdomain.objects.get(
								http_url=http_url,
								scan_history=scan_history
							)

							subdomain.waf.add(waf_obj)





def directory_fuzz(
		scan_history,
		activity_id,
		yaml_configuration,
		results_dir,
		domain=None,
		subdomain=None,
		filename=None,
		subscan=None
	):
	'''
		This function is responsible for performing directory scan, and currently
		uses ffuf as a default tool
	'''
	output_filename = filename if filename else 'dirs.json'
	dirs_results = results_dir + '/' + output_filename

	domain_name = domain.name if domain else subdomain

	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		send_notification('Directory Bruteforce has been initiated for {}.'.format(domain_name))

	# get wordlist
	if (WORDLIST not in yaml_configuration[DIR_FILE_FUZZ] or
		not yaml_configuration[DIR_FILE_FUZZ][WORDLIST] or
			'default' in yaml_configuration[DIR_FILE_FUZZ][WORDLIST]):
		wordlist_location = '/usr/src/wordlist/dicc.txt'
	else:
		wordlist_location = '/usr/src/wordlist/' + \
			yaml_configuration[DIR_FILE_FUZZ][WORDLIST] + '.txt'

	ffuf_command = 'ffuf -w ' + wordlist_location

	if domain:
		subdomains_fuzz = Subdomain.objects.filter(
			scan_history__id=scan_history.id).exclude(http_url__isnull=True)
	else:
		subdomains_fuzz = Subdomain.objects.filter(
			name=subdomain).filter(
			scan_history__id=scan_history.id)

	if USE_EXTENSIONS in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][USE_EXTENSIONS]:
		if EXTENSIONS in yaml_configuration[DIR_FILE_FUZZ]:
			extensions = ','.join('.' + str(ext) for ext in yaml_configuration[DIR_FILE_FUZZ][EXTENSIONS])

			ffuf_command = ' {} -e {} '.format(
				ffuf_command,
				extensions
			)

	if THREADS in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][THREADS] > 0:
		threads = yaml_configuration[DIR_FILE_FUZZ][THREADS]
		ffuf_command = ' {} -t {} '.format(
			ffuf_command,
			threads
		)

	if RECURSIVE in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][RECURSIVE]:
		recursive_level = yaml_configuration[DIR_FILE_FUZZ][RECURSIVE_LEVEL]
		ffuf_command = ' {} -recursion -recursion-depth {} '.format(
			ffuf_command,
			recursive_level
		)

	if STOP_ON_ERROR in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][STOP_ON_ERROR]:
		ffuf_command = '{} -se'.format(
			ffuf_command
		)

	if FOLLOW_REDIRECT in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][FOLLOW_REDIRECT]:
		ffuf_command = ' {} -fr '.format(
			ffuf_command
		)

	if AUTO_CALIBRATION in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][AUTO_CALIBRATION]:
		ffuf_command = ' {} -ac '.format(
			ffuf_command
		)

	if TIMEOUT in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][TIMEOUT] > 0:
		timeout = yaml_configuration[DIR_FILE_FUZZ][TIMEOUT]
		ffuf_command = ' {} -timeout {} '.format(
			ffuf_command,
			timeout
		)

	if DELAY in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][DELAY] > 0:
		delay = yaml_configuration[DIR_FILE_FUZZ][DELAY]
		ffuf_command = ' {} -p "{}" '.format(
			ffuf_command,
			delay
		)

	if MATCH_HTTP_STATUS in yaml_configuration[DIR_FILE_FUZZ]:
		mc = ','.join(str(code) for code in yaml_configuration[DIR_FILE_FUZZ][MATCH_HTTP_STATUS])
	else:
		mc = '200,204'

	ffuf_command = ' {} -mc {} '.format(
		ffuf_command,
		mc
	)

	if MAX_TIME in yaml_configuration[DIR_FILE_FUZZ] \
		and yaml_configuration[DIR_FILE_FUZZ][MAX_TIME] > 0:
		max_time = yaml_configuration[DIR_FILE_FUZZ][MAX_TIME]
		ffuf_command = ' {} -maxtime {} '.format(
			ffuf_command,
			max_time
		)

	if CUSTOM_HEADER in yaml_configuration and yaml_configuration[CUSTOM_HEADER]:
		ffuf_command += ' -H "{}"'.format(yaml_configuration[CUSTOM_HEADER])

	logger.info(ffuf_command)

	for subdomain in subdomains_fuzz:
		command = None
		# delete any existing dirs.json
		if os.path.isfile(dirs_results):
			os.system('rm -rf {}'.format(dirs_results))

		if subdomain.http_url:
			http_url = subdomain.http_url + 'FUZZ' if subdomain.http_url[-1:] == '/' else subdomain.http_url + '/FUZZ'
		else:
			http_url = subdomain

		# proxy
		proxy = get_random_proxy()
		if proxy:
			ffuf_command = "{} -x '{}'".format(
				ffuf_command,
				proxy
			)

		command = '{} -u {} -o {} -of json'.format(
			ffuf_command,
			http_url,
			dirs_results
		)

		logger.info(command)
		process = subprocess.Popen(command.split())
		process.wait()

		try:
			if os.path.isfile(dirs_results):
				with open(dirs_results, "r") as json_file:
					json_string = json.loads(json_file.read())
					subdomain = Subdomain.objects.get(
							scan_history__id=scan_history.id, http_url=subdomain.http_url)
					# TODO: URL Models to be created here
					# Create a directory Scan model
					directory_scan = DirectoryScan()
					directory_scan.scanned_date = timezone.now()
					directory_scan.command_line = json_string['commandline']
					directory_scan.save()

					for result in json_string['results']:
						# check if directory already exists else create a new one
						if DirectoryFile.objects.filter(
							name=result['input']['FUZZ'],
							length__exact=result['length'],
							lines__exact=result['lines'],
							http_status__exact=result['status'],
							words__exact=result['words'],
							url=result['url'],
							content_type=result['content-type'],
						).exists():
							file = DirectoryFile.objects.get(
								name=result['input']['FUZZ'],
								length__exact=result['length'],
								lines__exact=result['lines'],
								http_status__exact=result['status'],
								words__exact=result['words'],
								url=result['url'],
								content_type=result['content-type'],
							)
						else:
							file = DirectoryFile()
							file.name=result['input']['FUZZ']
							file.length=result['length']
							file.lines=result['lines']
							file.http_status=result['status']
							file.words=result['words']
							file.url=result['url']
							file.content_type=result['content-type']
							file.save()

						directory_scan.directory_files.add(file)

					if subscan:
						directory_scan.dir_subscan_ids.add(subscan)

					subdomain.directories.add(directory_scan)

		except Exception as exception:
			logger.error(exception)
			if not subscan:
				update_last_activity(activity_id, 0)
			raise Exception(exception)

	if notification and notification[0].send_scan_status_notif:
		send_notification('Directory Bruteforce has been completed for {}.'.format(domain_name))


def fetch_endpoints(
		scan_history,
		activity_id,
		yaml_configuration,
		results_dir,
		domain=None,
		subdomain=None,
		filename=None,
		subscan=None
	):
	'''
		This function is responsible for fetching all the urls associated with target
		and runs HTTP probe
		reNgine has ability to fetch deep urls, meaning url for all the subdomains
		but, when subdomain is given, subtask is running, deep or normal scan should
		not work, it should simply fetch urls for that subdomain
	'''

	if GF_PATTERNS in yaml_configuration[FETCH_URL]:
		scan_history.used_gf_patterns = ','.join(
			pattern for pattern in yaml_configuration[FETCH_URL][GF_PATTERNS])
		scan_history.save()

	logger.info('Initiated Endpoint Fetching')
	domain_name = domain.name if domain else subdomain
	output_filename = filename if filename else 'all_urls.txt'

	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		send_notification('reNgine is currently gathering endpoints for {}.'.format(domain_name))

	# Get tools for endpoint discovery
	DEFAULT_ENDPOINT_TOOLS = ['gauplus', 'hakrawler', 'waybackurls', 'gospider']
	tools = yaml_configuration[FETCH_URL].get(USES_TOOLS, DEFAULT_ENDPOINT_TOOLS)

	# Get scan intensity
	DEFAULT_SCAN_INTENSITY = 'normal'
	scan_intensity = yaml_configuration[FETCH_URL].get(INTENSITY, DEFAULT_SCAN_INTENSITY)

	valid_url_of_domain_regex = "\'https?://([a-z0-9]+[.])*{}.*\'".format(domain_name)

	alive_subdomains_path = f'{results_dir}/{output_filename}'
	sorted_subdomains_path = f'{results_dir}/sorted_subdomain_collection.txt'

	for tool in tools:
		if tool in ('gauplus', 'hakrawler', 'waybackurls'):
			if subdomain:
				subdomain_url = subdomain.http_url if subdomain.http_url else f'https://{subdomain.name}'
				input_target = f'echo {subdomain_url}'
			elif scan_intensity == 'deep' and domain:
				input_target = f'cat {sorted_subdomains_path}'
			else:
				input_target = f'echo {domain_name}'

		if tool == 'gauplus':
			logger.info('Running Gauplus')
			gauplus_command = f'{input_target} | gauplus --random-agent | grep -Eo {valid_url_of_domain_regex} > {results_dir}/urls_gau.txt'
			logger.info(gauplus_command)
			os.system(gauplus_command)

		elif tool == 'hakrawler':
			logger.info('Running hakrawler')
			hakrawler_command = f'{input_target} | hakrawler -subs -u | grep -Eo {valid_url_of_domain_regex} > {results_dir}/urls_hakrawler.txt'
			logger.info(hakrawler_command)
			os.system(hakrawler_command)

		elif tool == 'waybackurls':
			logger.info('Running waybackurls')
			waybackurls_command = f'{input_target} | waybackurls | grep -Eo {valid_url_of_domain_regex} > {results_dir}/urls_waybackurls.txt'
			logger.info(waybackurls_command)
			os.system(waybackurls_command)

		elif tool == 'gospider':
			logger.info('Running gospider')
			if subdomain:
				subdomain_url = subdomain.http_url if subdomain.http_url else 'https://' + subdomain.name
				gospider_command = f'gospider -s {subdomain_url}'
			elif scan_intensity == 'deep' and domain:
				gospider_command = f'gospider -S {alive_subdomains_path}'
			else:
				gospider_command = f'gospider -s https://{domain_name} '

			gospider_command += f' --js -t 100 -d 2 --sitemap --robots -w -r | grep -Eo {valid_url_of_domain_regex} > {results_dir}/urls_gospider.txt'
			logger.info(gospider_command)
			os.system(gospider_command)

	# Cleanup urls files
	os.system(f'cat {results_dir}/urls* > {results_dir}/final_urls.txt')
	os.system(f'rm -rf {results_dir}/url*')
	
	# Sorting and unique urls
	logger.info("Sort and Unique")
	if domain:
		os.system(f'cat {results_dir}/alive.txt >> {results_dir}/final_urls.txt')
	os.system(f'sort -u {results_dir}/final_urls.txt -o {results_dir}/{output_filename}')

	if IGNORE_FILE_EXTENSION in yaml_configuration[FETCH_URL]:
		ignore_extension = '|'.join(
			yaml_configuration[FETCH_URL][IGNORE_FILE_EXTENSION])
		logger.info('Ignore extensions ' + ignore_extension)
		os.system(
			f'cat {results_dir}/{output_filename} | grep -Eiv "\\.({ignore_extension}).*" > {results_dir}/temp_urls.txt')
		os.system(
			f'rm {results_dir}/{output_filename} && mv {results_dir}/temp_urls.txt {results_dir}/{output_filename}')

	'''
	Store all the endpoints and then run the httpx
	'''
	domain_obj = None
	if domain:
		domain_obj = domain
	elif subdomain:
		domain_obj = subdomain.target_domain

	try:
		endpoint_final_url = f'{results_dir}/{output_filename}'
		if not os.path.isfile(endpoint_final_url):
			return

		with open(endpoint_final_url) as endpoint_list:
			for url in endpoint_list:
				http_url = url.rstrip('\n')
				if not EndPoint.objects.filter(scan_history=scan_history, http_url=http_url).exists():
					_subdomain = get_subdomain_from_url(http_url)
					if Subdomain.objects.filter(
							scan_history=scan_history).filter(
							name=_subdomain).exists():
						subdomain = Subdomain.objects.get(
							scan_history=scan_history, name=_subdomain)
					else:
						'''
							gau or gosppider can gather interesting endpoints which
							when parsed can give subdomains that were not existent from
							subdomain scan. so storing them
						'''
						logger.error(
							f'Subdomain {_subdomain} not found, adding...')
						subdomain_dict = DottedDict({
							'scan_history': scan_history,
							'target_domain': domain_obj,
							'name': _subdomain,
						})
						subdomain = save_subdomain(subdomain_dict)
					endpoint_dict = DottedDict({
						'scan_history': scan_history,
						'target_domain': domain_obj,
						'subdomain': subdomain,
						'http_url': http_url,
						'subscan': subscan
					})
					save_endpoint(endpoint_dict)
	except Exception as e:
		logger.error(e)
		if not subscan:
			update_last_activity(activity_id, 0)
		raise Exception(exception)

	if notification and notification[0].send_scan_output_file:
		send_files_to_discord(results_dir + '/{}'.format(output_filename))

	'''
	TODO:
	Go spider & waybackurls accumulates a lot of urls, which is good but nuclei
	takes forever to scan even a simple website, so we will do http probing
	and filter HTTP status 404, this way we can reduce the number of Non Existent
	URLS
	'''
	logger.info('HTTP Probing on collected endpoints')

	httpx_command = f'/go/bin/httpx -l {results_dir}/{output_filename} -status-code -content-length -ip -cdn -title -tech-detect -json -follow-redirects -random-agent -o {results_dir}/final_httpx_urls.json'

	proxy = get_random_proxy()
	if proxy:
		httpx_command += " --http-proxy {} ".format(proxy)

	if CUSTOM_HEADER in yaml_configuration and yaml_configuration[CUSTOM_HEADER]:
		httpx_command += ' -H "{}" '.format(yaml_configuration[CUSTOM_HEADER])

	logger.info(httpx_command)
	os.system(remove_cmd_injection_chars(httpx_command))

	url_results_file = f'{results_dir}/final_httpx_urls.json'
	try:
		if os.path.isfile(url_results_file):
			urls_json_result = open(url_results_file, 'r')
			lines = urls_json_result.readlines()
			for line in lines:
				json_st = json.loads(line.strip())
				http_url = json_st['url']
				_subdomain = get_subdomain_from_url(http_url)

				if Subdomain.objects.filter(
						scan_history=scan_history).filter(
						name=_subdomain).exists():
					subdomain_obj = Subdomain.objects.get(
						scan_history=scan_history, name=_subdomain)
				else:
					subdomain_dict = DottedDict({
						'scan_history': scan_history,
						'target_domain': domain,
						'name': _subdomain,
					})
					subdomain_obj = save_subdomain(subdomain_dict)

				if EndPoint.objects.filter(
						scan_history=scan_history).filter(
						http_url=http_url).exists():

					endpoint = EndPoint.objects.get(
						scan_history=scan_history, http_url=http_url)
				else:
					endpoint = EndPoint()
					endpoint_dict = DottedDict({
						'scan_history': scan_history,
						'target_domain': domain,
						'http_url': http_url,
						'subdomain': subdomain_obj
					})
					endpoint = save_endpoint(endpoint_dict)

				if 'title' in json_st:
					endpoint.page_title = json_st['title']
				if 'webserver' in json_st:
					endpoint.webserver = json_st['webserver']
				if 'content-length' in json_st:
					endpoint.content_length = json_st['content-length']
				if 'content-type' in json_st:
					endpoint.content_type = json_st['content-type']
				if 'status-code' in json_st:
					endpoint.http_status = json_st['status-code']
				if 'response-time' in json_st:
					response_time = float(''.join(ch for ch in json_st['response-time'] if not ch.isalpha()))
					if json_st['response-time'][-2:] == 'ms':
						response_time = response_time / 1000
					endpoint.response_time = response_time
				endpoint.save()
				if 'technologies' in json_st:
					for _tech in json_st['technologies']:
						if Technology.objects.filter(name=_tech).exists():
							tech = Technology.objects.get(name=_tech)
						else:
							tech = Technology(name=_tech)
							tech.save()
						endpoint.technologies.add(tech)
						# get subdomain object
						subdomain = Subdomain.objects.get(
							scan_history=scan_history,
							name=_subdomain
						)
						subdomain.technologies.add(tech)
						subdomain.save()
	except Exception as exception:
		logger.error(exception)
		if not subscan:
			update_last_activity(activity_id, 0)
		raise Exception(exception)

	if notification and notification[0].send_scan_status_notif:
		endpoint_count = EndPoint.objects.filter(
			scan_history__id=scan_history.id).values('http_url').distinct().count()
		endpoint_alive_count = EndPoint.objects.filter(
				scan_history__id=scan_history.id, http_status__exact=200).values('http_url').distinct().count()
		send_notification(f'reNgine has finished gathering endpoints for {domain_name} and has discovered *{endpoint_count}* unique endpoints.\n\n{endpoint_alive_count} of those endpoints reported HTTP status 200.')


	# once endpoint is saved, run gf patterns TODO: run threads
	if GF_PATTERNS in yaml_configuration[FETCH_URL]:
		for pattern in yaml_configuration[FETCH_URL][GF_PATTERNS]:
			# TODO: js var is causing issues, removing for now
			if pattern != 'jsvar':
				logger.info(f'Running GF for {pattern}')
				gf_output_file_path = f'{results_dir}/gf_patterns_{pattern}.txt'
				gf_command = f'cat {results_dir}/{output_filename} | gf {pattern} | grep -Eo {valid_url_of_domain_regex} >> {gf_output_file_path} '
				logger.info(gf_command)
				os.system(gf_command)
				if os.path.exists(gf_output_file_path):
					with open(gf_output_file_path) as gf_output:
						for line in gf_output:
							url = line.rstrip('\n')
							try:
								endpoint = EndPoint.objects.get(
									scan_history=scan_history, http_url=url)
								earlier_pattern = endpoint.matched_gf_patterns
								new_pattern = earlier_pattern + ',' + pattern if earlier_pattern else pattern
								endpoint.matched_gf_patterns = new_pattern
							except Exception as e:
								# add the url in db
								logger.error(e)
								logger.info('Adding URL ' + url)
								endpoint = EndPoint()
								endpoint.http_url = url
								endpoint.target_domain = domain
								endpoint.scan_history = scan_history
								try:
									_subdomain = Subdomain.objects.get(
										scan_history=scan_history,
										name=get_subdomain_from_url(url)
									)
									endpoint.subdomain = _subdomain
								except Exception as e:
									continue
								endpoint.matched_gf_patterns = pattern
							finally:
								endpoint.save()

					os.system(f'rm -rf {gf_output_file_path}')


def vulnerability_scan(
		scan_history,
		activity_id,
		yaml_configuration,
		results_dir,
		domain=None,
		subdomain=None,
		filename=None,
		subscan=None
	):
	logger.info('Initiating Vulnerability Scan')
	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		if domain:
			send_notification('Vulnerability scan has been initiated for {}.'.format(domain.name))
		elif subdomain:
			send_notification('Vulnerability scan has been initiated for {}.'.format(subdomain.name))
	'''
	This function will run nuclei as a vulnerability scanner
	----
	unfurl the urls to keep only domain and path, this will be sent to vuln scan
	ignore certain file extensions
	Thanks: https://github.com/six2dez/reconftw
	'''
	output_filename = filename if filename else 'vulnerability.json'
	vulnerability_result_path = f'{results_dir}/{output_filename}'


	if domain:
		urls_path = '/alive.txt'

		# TODO: create a object in scan engine, to say deep scan then only use unfurl, otherwise it is time consuming

		# if scan_history.scan_type.fetch_url:
		#     os.system('cat {0}/all_urls.txt | grep -Eiv "\\.(eot|jpg|jpeg|gif|css|tif|tiff|png|ttf|otf|woff|woff2|ico|pdf|svg|txt|js|doc|docx)$" | unfurl -u format %s://%d%p >> {0}/unfurl_urls.txt'.format(results_dir))
		#     os.system(
		#         'sort -u {0}/unfurl_urls.txt -o {0}/unfurl_urls.txt'.format(results_dir))
		#     urls_path = '/unfurl_urls.txt'

		vulnerability_scan_input_file = results_dir + urls_path
		nuclei_command = f'nuclei -json -l {vulnerability_scan_input_file} -o {vulnerability_result_path}'
	else:
		url_to_scan = subdomain.http_url if subdomain.http_url else 'https://' + subdomain.name
		nuclei_command = f'nuclei -json -u {url_to_scan} -o {vulnerability_result_path}'
		domain_id = scan_history.domain.id
		domain = Domain.objects.get(id=domain_id)

	# check nuclei config
	use_nuclei_conf = yaml_configuration[VULNERABILITY_SCAN].get(USE_NUCLEI_CONFIG, False)
	if use_nuclei_conf:
		nuclei_command += ' -config /root/.config/nuclei/config.yaml'

	'''
	Nuclei Templates
	Either custom template has to be supplied or default template, if neither has
	been supplied then use all templates including custom templates
	'''
	custom_nuclei_template = yaml_configuration[VULNERABILITY_SCAN].get(CUSTOM_NUCLEI_TEMPLATE, None)
	nuclei_template = yaml_configuration[VULNERABILITY_SCAN].get(NUCLEI_TEMPLATE, None)
	if custom_nuclei_template or nuclei_template:
		if custom_nuclei_template:
			_template = ','.join(
				[str(element) + '.yaml' for element in yaml_configuration[VULNERABILITY_SCAN][CUSTOM_NUCLEI_TEMPLATE]])
			template = _template.replace(',', ' -t ')
			nuclei_command += f' -t {template}'

		elif nuclei_template:
			if ALL in nuclei_template:
				template = NUCLEI_TEMPLATES_PATH
			else:
				_template = ','.join([NUCLEI_TEMPLATES_PATH + str(element)
									  for element in yaml_configuration[VULNERABILITY_SCAN][NUCLEI_TEMPLATE]])
				template = _template.replace(',', ' -t ')
			nuclei_command += f' -t {template}'
	else:
		nuclei_command += f' -t /root/nuclei-templates'

	'''
	Concurrency.
	'''
	concurrency = yaml_configuration[VULNERABILITY_SCAN].get(NUCLEI_CONCURRENCY, 0)
	if concurrency > 0:
		nuclei_command += f' -c {str(concurrency)}'

	'''
	Rate limit
	'''
	rate_limit = yaml_configuration[VULNERABILITY_SCAN].get(RATE_LIMIT, 0)
	if rate_limit > 0:
		nuclei_command += f' -rl {str(rate_limit)}'


	'''
	Timeout
	'''
	timeout = yaml_configuration[VULNERABILITY_SCAN].get(TIMEOUT, 0)
	if timeout > 0:
		nuclei_command += f' -timeout {str(timeout)}'

	'''
	Retries
	'''
	retries = yaml_configuration[VULNERABILITY_SCAN].get(RETRIES, 0)
	if retries > 0:
		nuclei_command += f' -retries {str(retries)}'

	'''
	Custom headers
	'''
	if CUSTOM_HEADER in yaml_configuration and yaml_configuration[CUSTOM_HEADER]:
		nuclei_command += ' -H "{}" '.format(yaml_configuration[CUSTOM_HEADER])

	'''
	Severity
	'''
	severities = yaml_configuration[VULNERABILITY_SCAN].get(NUCLEI_SEVERITY, ["critical", "high", "medium", "low", "info", "unknown"])
	logger.info('Updating Nuclei Templates!')
	os.system('nuclei -update-templates')
	for severity in severities:
		# delete any existing vulnerability.json file
		if os.path.isfile(vulnerability_result_path):
			os.system(f'rm {vulnerability_result_path}')
		
		# run nuclei
		cmd = f'{nuclei_command} -severity {severity}'

		proxy = get_random_proxy()
		if proxy:
			cmd += f' -proxy {proxy} '

		logger.info('Running Nuclei Scanner!')
		logger.info(cmd)
		process = subprocess.Popen(cmd.split())
		process.wait()

		try:
			if os.path.isfile(vulnerability_result_path):
				urls_json_result = open(vulnerability_result_path, 'r')
				lines = urls_json_result.readlines()
				for line in lines:
					json_st = json.loads(line.strip())
					host = json_st['host']
					_subdomain = get_subdomain_from_url(host)
					try:
						subdomain = Subdomain.objects.get(
							name=_subdomain, scan_history=scan_history)
						vulnerability = Vulnerability()
						vulnerability.subdomain = subdomain
						vulnerability.scan_history = scan_history
						vulnerability.target_domain = domain
						vuln_str = json_st['info'].get('severity', 'info')

						# Build vulnerability object
						vulnerability.endpoint = endpoint
						vulnerability.template = json_st['template']
						vulnerability.template_url = json_st['template-url']
						vulnerability.template_id = json_st['template-id']
						vulnerability.name = json_st['info'].get('name', '')
						vulnerability.severity = NUCLEI_SEVERITY_MAP[vuln_str]
						vulnerability.description = json_st['info'].get('description' ,'')
						vulnerability.matcher_name = json_st.get('matcher-name')
						vulnerability.http_url = json_st.get('matched-at')
						vulnerability.curl_command = json_st.get('curl-command')
						vulnerability.extracted_results = json_st.get('extracted-results', [])
						vulnerability.type = json_st['type']
						vulnerability.cvss_metrics = json_st['info'].get('classification', {}).get('cvss-metrics', '')
						vulnerability.cvss_score = json_st['info'].get('classification', {}).get('cvss-score', '')
						vulnerability.discovered_date = timezone.now()
						vulnerability.open_status = True
						vulnerability.save()

						# Create endpoint from host
						endpoint_query = Endpoint.objects.filter(scan_history=scan_history).filter(target_domain=domain).filter(http_url=host)
						endpoint = None
						if endpoint_query.exists():
							endpoint = EndPoint.objects.get(
								scan_history=scan_history,
								target_domain=domain,
								http_url=host
							)
						else:
							logger.info('Creating Endpoint...')
							endpoint_dict = DottedDict({
								'scan_history': scan_history,
								'target_domain': domain,
								'http_url': host,
								'subdomain': subdomain
							})
							endpoint = save_endpoint(endpoint_dict)
							logger.info(f'Endpoint {host} created!')
						vulnerability.endpoint = endpoint

						# Create endpoint from http_url
						endpoint_query = Endpoint.objects.filter(scan_history=scan_history).filter(target_domain=domain).filter(http_url=vulnerability.http_url)
						if vulnerability.http_url and not endpoint_query.exists():
							logger.info('Creating Endpoint...')
							endpoint_dict = DottedDict({
								'scan_history': scan_history,
								'target_domain': domain,
								'http_url': vulnerability.http_url,
								'subdomain': subdomain
							})
							save_endpoint(endpoint_dict)
							logger.info('Endpoint {} created!'.format(vulnerability.http_url))

						# Save tags
						tags = json_st['info'].get('tags', [])
						for tag in tags:
							if VulnerabilityTags.objects.filter(name=tag).exists():
								tag = VulnerabilityTags.objects.get(name=tag)
							else:
								tag = VulnerabilityTags(name=tag)
								tag.save()
							vulnerability.tags.add(tag)

						# Save CVEs
						cve_ids = json_st['info'].get('classification', {}).get('cve-id', [])
						for cve in cve_ids:
							if CveId.objects.filter(name=cve).exists():
								cve_obj = CveId.objects.get(name=cve)
							else:
								cve_obj = CveId(name=cve)
								cve_obj.save()
							vulnerability.cve_ids.add(cve_obj)

						# Save CWEs
						cwe_ids = json_st['info'].get('classification', {}).get('cwe-id', [])
						for cwe in cwe_ids:
							if CweId.objects.filter(name=cwe).exists():
								cwe_obj = CweId.objects.get(name=cwe)
							else:
								cwe_obj = CweId(name=cwe)
								cwe_obj.save()
							vulnerability.cwe_ids.add(cwe_obj)

						# Save vuln reference
						references = json_st['info'].get('reference', [])
						for ref_url in references:
							if VulnerabilityReference.objects.filter(url=ref_url).exists():
								reference = VulnerabilityReference.objects.get(url=ref_url)
							else:
								reference = VulnerabilityReference(url=ref_url)
								reference.save()
							vulnerability.references.add(reference)

						# Save subscan id in vulnerability object
						if subscan:
							vulnerability.vuln_subscan_ids.add(subscan)
						
						# Save vulnerability object
						vulnerability.save()

						# Send notification for all vulnerabilities except info
						if vulnerability.severity != 0 and notification and notification[0].send_vuln_notif:
							severity_str = json_st['info'].get('severity', 'info')
							message = "*Alert: Vulnerability Identified*"
							message += "\n\n"
							message += f'A *{severity_str}* severity vulnerability has been identified.'
							message += f'\nVulnerability Name: {vulnerability.name}'
							message += f'\nVulnerable URL: {vulnerability.host}'
							send_notification(message)

						# Send report to hackerone
						if Hackerone.objects.all().exists() and severity_str not in ('info', 'low') and vulnerability.target_domain.h1_team_handle:
							hackerone = Hackerone.objects.all()[0]
							if hackerone.send_critical and severity_str == 'critical':
								send_hackerone_report(vulnerability.id)
							elif hackerone.send_high and severity_str == 'high':
								send_hackerone_report(vulnerability.id)
							elif hackerone.send_medium and severity_str == 'medium':
								send_hackerone_report(vulnerability.id)
					except ObjectDoesNotExist:
						logger.error('Object not found')

		except Exception as exception:
			logger.error(exception)
			if not subscan:
				update_last_activity(activity_id, 0)
			raise Exception(exception)

	if notification and notification[0].send_scan_status_notif:
		info_count = Vulnerability.objects.filter(
			scan_history__id=scan_history.id, severity=0).count()
		low_count = Vulnerability.objects.filter(
			scan_history__id=scan_history.id, severity=1).count()
		medium_count = Vulnerability.objects.filter(
			scan_history__id=scan_history.id, severity=2).count()
		high_count = Vulnerability.objects.filter(
			scan_history__id=scan_history.id, severity=3).count()
		critical_count = Vulnerability.objects.filter(
			scan_history__id=scan_history.id, severity=4).count()
		unknown_count = Vulnerability.objects.filter(
			scan_history__id=scan_history.id, severity=-1).count()
		vulnerability_count = info_count + low_count + medium_count + high_count + critical_count + unknown_count

		message = f'Vulnerability scan has been completed for {domain.name} and discovered {vulnerability_count} vulnerabilities.'
		message += '\n\n*Vulnerability Stats:*'
		message += f'\nCritical: {critical_count}'
		message += f'\nHigh: {high_count}'
		message += f'\nMedium: {medium_count}'
		message += f'\nLow: {low_count}'
		message += f'\nInfo: {info_count}'
		message += f'\nUnknown: {unknown_count}'

		send_notification(message)


def scan_failed(scan_history):
	scan_history.scan_status = 0
	scan_history.stop_scan_date = timezone.now()
	scan_history.save()


def create_scan_activity(scan_history, message, status):
	scan_activity = ScanActivity()
	scan_activity.scan_of = scan_history
	scan_activity.title = message
	scan_activity.time = timezone.now()
	scan_activity.status = status
	scan_activity.save()
	return scan_activity.id


def update_last_activity(id, activity_status, error_message=None):
	ScanActivity.objects.filter(
		id=id).update(
		status=activity_status,
		error_message=error_message,
		time=timezone.now())


def delete_scan_data(results_dir):
	os.system(f'find {results_dir} -name "*.txt" -type f -delete')
	os.system(f'find {results_dir} -name "*.html" -type f -delete')
	os.system(f'find {results_dir} -name "*.json" -type f -delete')


def save_subdomain(subdomain_dict):
	subdomain = Subdomain()
	subdomain.discovered_date = timezone.now()
	subdomain.target_domain = subdomain_dict.get('target_domain')
	subdomain.scan_history = subdomain_dict.get('scan_history')
	subdomain.name = subdomain_dict.get('name')
	subdomain.http_url = subdomain_dict.get('http_url')
	subdomain.screenshot_path = subdomain_dict.get('screenshot_path')
	subdomain.http_header_path = subdomain_dict.get('http_header_path')
	subdomain.cname = subdomain_dict.get('cname')
	subdomain.is_cdn = subdomain_dict.get('is_cdn')
	subdomain.content_type = subdomain_dict.get('content_type')
	subdomain.webserver = subdomain_dict.get('webserver')
	subdomain.page_title = subdomain_dict.get('page_title')
	subdomain.is_imported_subdomain = subdomain_dict.get('is_imported_subdomain', False)
	subdomain.http_status = subdomain_dict.get('http_status', 0)
	subdomain.response_time = subdomain_dict.get('response_time')
	subdomain.content_length = subdomain_dict.get('content_length')
	subdomain.save()
	return subdomain


def save_endpoint(endpoint_dict):
	endpoint = EndPoint()
	endpoint.discovered_date = timezone.now()
	endpoint.scan_history = endpoint_dict.get('scan_history')
	endpoint.target_domain = endpoint_dict.get('target_domain')
	endpoint.subdomain = endpoint_dict.get('subdomain')
	endpoint.http_url = endpoint_dict.get('http_url')
	endpoint.page_title = endpoint_dict.get('page_title')
	endpoint.content_type = endpoint_dict.get('content_type')
	endpoint.webserver = endpoint_dict.get('webserver')
	endpoint.response_time = endpoint_dict.get('response_time', 0)
	endpoint.http_status = endpoint_dict.get('http_status', 0)
	endpoint.content_length = endpoint_dict.get('content_length', 0)
	endpoint.is_default = endpoint_dict.get('is_default', False)
	endpoint.save()

	if endpoint_dict.get('subscan'):
		endpoint.endpoint_subscan_ids.add(endpoint_dict.get('subscan'))
		endpoint.save()

	return endpoint


def perform_osint(scan_history, domain, yaml_configuration, results_dir):
	notification = Notification.objects.all()
	if notification and notification[0].send_scan_status_notif:
		send_notification(f'reNgine has initiated OSINT on target {domain.name}')

	if 'discover' in yaml_configuration[OSINT]:
		osint_discovery(scan_history, domain, yaml_configuration, results_dir)

	if 'dork' in yaml_configuration[OSINT]:
		dorking(scan_history, yaml_configuration)

	if notification and notification[0].send_scan_status_notif:
		send_notification(f'reNgine has completed performing OSINT on target {domain.name}')


def osint_discovery(scan_history, domain, yaml_configuration, results_dir):
	DEFAULT_OSINT_LOOKUP = ['emails', 'metainfo', 'employees']
	osint_lookup = yaml_configuration[OSINT].get(OSINT_DISCOVER, DEFAULT_OSINT_LOOKUP)

	if 'metainfo' in osint_lookup:
		osint_intensity = yaml_configuration[OSINT].get(INTENSITY, 'normal')
		documents_limit = yaml_configuration[OSINT].get(OSINT_DOCUMENTS_LIMIT, 50)
		if osint_intensity == 'normal':
			meta_dict = DottedDict({
				'osint_target': domain.name,
				'domain': domain,
				'scan_id': scan_history,
				'documents_limit': documents_limit
			})
			get_and_save_meta_info(meta_dict)
		elif osint_intensity == 'deep':
			# get all subdomains in scan_id
			subdomains = Subdomain.objects.filter(scan_history=scan_history)
			for subdomain in subdomains:
				meta_dict = DottedDict({
					'osint_target': subdomain.name,
					'domain': domain,
					'scan_id': scan_history,
					'documents_limit': documents_limit
				})
				get_and_save_meta_info(meta_dict)

	if 'emails' in osint_lookup:
		get_and_save_emails(scan_history, results_dir)
		get_and_save_leaked_credentials(scan_history, results_dir)

	if 'employees' in osint_lookup:
		get_and_save_employees(scan_history, results_dir)

def dorking(scan_history, yaml_configuration):
	# Some dork sources: https://github.com/six2dez/degoogle_hunter/blob/master/degoogle_hunter.sh
	# look in stackoverflow
	DEFAULT_DORKS = [
		'stackoverflow', 
		'3rdparty', 
		'social_media', 
		'project_management', 
		'code_sharing', 
		'config_files',
		'jenkins',
		'cloud_buckets',
		'php_error',
		'exposed_documents',
		'struts_rce',
		'db_files',
		'traefik',
		'git_exposed'
	]
	dorks = yaml_configuration[OSINT].get(OSINT_DORK, DEFAULT_DORKS)

	for dork in dorks:
		if dork == 'stackoverflow':
			dork_name = 'site:stackoverflow.com'
			dork_type = 'stackoverflow'
			get_and_save_dork_results(
				dork,
				dork_type,
				scan_history,
				in_target=False
			)

		elif dork == '3rdparty' :
			# look in 3rd party sitee
			dork_type = '3rdparty'
			lookup_websites = [
				'gitter.im',
				'papaly.com',
				'productforums.google.com',
				'coggle.it',
				'replt.it',
				'ycombinator.com',
				'libraries.io',
				'npm.runkit.com',
				'npmjs.com',
				'scribd.com',
				'gitter.im'
			]
			dork_name = ''
			for website in lookup_websites:
				dork_name = dork + ' | ' + 'site:' + website
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=False
				)

		elif dork == 'social_media' :
			dork_type = 'Social Media'
			social_websites = [
				'tiktok.com',
				'facebook.com',
				'twitter.com',
				'youtube.com',
				'pinterest.com',
				'tumblr.com',
				'reddit.com'
			]
			dork_name = ''
			for website in social_websites:
				dork_name = dork + ' | ' + 'site:' + website
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=False
				)

		elif dork == 'project_management' :
			dork_type = 'Project Management'
			project_websites = [
				'trello.com',
				'*.atlassian.net'
			]
			dork_name = ''
			for website in project_websites:
				dork_name = dork + ' | ' + 'site:' + website
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=False
				)

		elif dork == 'code_sharing' :
			dork_type = 'Code Sharing Sites'
			code_websites = [
				'github.com',
				'gitlab.com',
				'bitbucket.org'
			]
			dork_name = ''
			for website in code_websites:
				dork_name = dork + ' | ' + 'site:' + website
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=False
				)

		elif dork == 'config_files' :
			dork_type = 'Config Files'
			config_file_ext = [
				'env',
				'xml',
				'conf',
				'cnf',
				'inf',
				'rdp',
				'ora',
				'txt',
				'cfg',
				'ini'
			]

			dork_name = ''
			for extension in config_file_ext:
				dork_name = dork + ' | ' + 'ext:' + extension
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=True
				)

		if dork == 'jenkins' :
			dork_type = 'Jenkins'
			dork_name = 'intitle:\"Dashboard [Jenkins]\"'
			get_and_save_dork_results(
				dork_name,
				dork_type,
				scan_history,
				in_target=True
			)

		elif dork == 'wordpress_files' :
			dork_type = 'Wordpress Files'
			inurl_lookup = [
				'wp-content',
				'wp-includes'
			]

			dork_name = ''
			for lookup in inurl_lookup:
				dork_name = dork + ' | ' + 'inurl:' + lookup
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=True
				)

		elif dork == 'cloud_buckets':
			dork_type = 'Cloud Buckets'
			cloud_websites = [
				'.s3.amazonaws.com',
				'storage.googleapis.com',
				'amazonaws.com'
			]

			dork_name = ''
			for website in cloud_websites:
				dork_name = dork + ' | ' + 'site:' + website
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=False
				)

		elif dork == 'php_error':
			dork_type = 'PHP Error'
			error_words = [
				'\"PHP Parse error\"',
				'\"PHP Warning\"',
				'\"PHP Error\"'
			]

			dork_name = ''
			for word in error_words:
				dork_name = dork + ' | ' + word
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=True
				)

		elif dork == 'exposed_documents':
			dork_type = 'Exposed Documents'
			docs_file_ext = [
				'doc',
				'docx',
				'odt',
				'pdf',
				'rtf',
				'sxw',
				'psw',
				'ppt',
				'pptx',
				'pps',
				'csv'
			]

			dork_name = ''
			for extension in docs_file_ext:
				dork_name = dork + ' | ' + 'ext:' + extension
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=True
				)

		elif dork == 'struts_rce':
			dork_type = 'Apache Struts RCE'
			struts_file_ext = [
				'action',
				'struts',
				'do'
			]

			dork_name = ''
			for extension in struts_file_ext:
				dork_name = dork + ' | ' + 'ext:' + extension
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=True
				)

		elif dork == 'db_files':
			dork_type = 'Database Files'
			db_file_ext = [
				'sql',
				'db',
				'dbf',
				'mdb'
			]

			dork_name = ''
			for extension in db_file_ext:
				dork_name = dork_name + ' | ' + 'ext:' + extension
				get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					scan_history,
					in_target=True
				)

		elif dork == 'traefik':
			dork_name = 'intitle:traefik inurl:8080/dashboard'
			dork_type = 'Traefik'
			get_and_save_dork_results(
				dork_name,
				dork_type,
				scan_history,
				in_target=True
			)

		elif dork == 'git_exposed':
			dork_name = 'inurl:\"/.git\"'
			dork_type = '.git Exposed'
			get_and_save_dork_results(
				dork_name,
				dork_type,
				scan_history,
				in_target=True
			)


def get_and_save_dork_results(dork, type, scan_history, in_target=False):
	degoogle_obj = degoogle.dg()
	proxy = get_random_proxy()
	if proxy:
		os.environ['https_proxy'] = proxy
		os.environ['HTTPS_PROXY'] = proxy

	if in_target:
		query = f'{dork} site:{scan_history.domain.name}'
	else:
		query = f'{dork} \"{scan_history.domain.name}\"'
	logger.info(query)
	degoogle_obj.query = query
	results = degoogle_obj.run()
	logger.info(results)
	for result in results:
		dork, _ = Dork.objects.get_or_create(
			type=type,
			description=result['desc'],
			url=result['url']
		)
		scan_history.dorks.add(dork)


def get_and_save_employees(scan_history, results_dir):
	theHarvester_dir = '/usr/src/github/theHarvester'

	# Update proxies.yaml
	if Proxy.objects.all().exists():
		proxy = Proxy.objects.all()[0]
		if proxy.use_proxy:
			proxy_list = proxy.proxies.splitlines()
			yaml_data = {'http' : proxy_list}
			with open(f'{theHarvester_dir}/proxies.yaml', 'w') as file:
				yaml.dump(yaml_data, file)

	os.system(f'cd {theHarvester_dir} && python3 theHarvester.py -d {scan_history.domain.name} -b all -f {results_dir}/theHarvester.html')

	file_location = f'{results_dir}/theHarvester.html'

	# Delete proxy environ var
	os.environ.pop('https_proxy')
	os.environ.pop('HTTPS_PROXY')

	if os.path.isfile(file_location):
		logger.info('Parsing theHarvester results')
		options = FirefoxOptions()
		options.add_argument("--headless")
		driver = webdriver.Firefox(options=options)
		driver.get(f'file://{file_location}')
		tabledata = driver.execute_script('return tabledata')
		# save email addresses and linkedin employees
		for data in tabledata:
			if data['record'] == 'email':
				_email = data['result']
				email, _ = Email.objects.get_or_create(address=_email)
				scan_history.emails.add(email)
			elif data['record'] == 'people':
				_employee = data['result']
				split_val = _employee.split('-')
				name = split_val[0]
				if len(split_val) == 2:
					designation = split_val[1]
				else:
					designation = ""
				employee, _ = Employee.objects.get_or_create(name=name, designation=designation)
				scan_history.employees.add(employee)
		driver.quit()
		print(tabledata)


def get_and_save_emails(scan_history, results_dir):

	# get email address
	proxy = get_random_proxy()
	if proxy:
		os.environ['https_proxy'] = proxy
		os.environ['HTTPS_PROXY'] = proxy

	emails = []

	try:
		logger.info('OSINT: Getting emails from Google')
		email_from_google = get_emails_from_google(scan_history.domain.name)
		logger.info('OSINT: Getting emails from Bing')
		email_from_bing = get_emails_from_bing(scan_history.domain.name)
		logger.info('OSINT: Getting emails from Baidu')
		email_from_baidu = get_emails_from_baidu(scan_history.domain.name)
		emails = list(set(email_from_google + email_from_bing + email_from_baidu))
		logger.info(emails)
	except Exception as e:
		logger.error(e)

	# Write to file
	leak_target_path = f'{results_dir}/creds_target.txt'
	with open(leak_target_path, 'w') as leak_target_file:
		for _email in emails:
			email, _ = Email.objects.get_or_create(address=_email)
			scan_history.emails.add(email)
			leak_target_file.write(f'{_email}\n')

		# fill leak_target_file with possible email address
		leak_target_file.write(f'%@{scan_history.domain.name}\n')
		leak_target_file.write(f'%@%.{scan_history.domain.name}\n')
		leak_target_file.write(f'%.%@{scan_history.domain.name}\n')
		leak_target_file.write(f'%.%@%.{scan_history.domain.name}\n')
		leak_target_file.write(f'%_%@{scan_history.domain.name}\n')
		leak_target_file.write(f'%_%@%.{scan_history.domain.name}\n')


def get_and_save_leaked_credentials(scan_history, results_dir):
	logger.info('OSINT: Getting leaked credentials...')

	leak_target_file = f'{results_dir}/creds_target.txt'
	leak_output_file = f'{results_dir}/pwndb.json'

	pwndb_command = f'python3 /usr/src/github/pwndb/pwndb.py --proxy tor:9150 --output json --list {leak_target_file}'

	try:
		pwndb_output = subprocess.getoutput(pwndb_command)
		creds = json.loads(pwndb_output)

		for cred in creds:
			if cred['username'] != 'donate':
				email_id = f"{cred['username']}@{cred['domain']}"

				email_obj, _ = Email.objects.get_or_create(
					address=email_id,
				)
				email_obj.password = cred['password']
				email_obj.save()
				scan_history.emails.add(email_obj)
	except Exception as e:
		logger.error(e)
		pass


def get_and_save_meta_info(meta_dict):
	logger.info(f'Getting METADATA for {meta_dict.osint_target}')
	proxy = get_random_proxy()
	if proxy:
		os.environ['https_proxy'] = proxy
		os.environ['HTTPS_PROXY'] = proxy
	result = metadata_extractor.extract_metadata_from_google_search(meta_dict.osint_target, meta_dict.documents_limit)
	if not result:
		return
	results = result.get_metadata()
	for meta in results:
		meta_finder_document = MetaFinderDocument()
		subdomain = Subdomain.objects.get(scan_history=meta_dict.scan_id, name=meta_dict.osint_target)
		meta_finder_document.subdomain = subdomain
		meta_finder_document.target_domain = meta_dict.domain
		meta_finder_document.scan_history = meta_dict.scan_id

		item = DottedDict(results[meta])
		metadata = results[meta]['metadata']
		meta_finder_document.url = item.url
		meta_finder_document.doc_name = meta
		meta_finder_document.http_status = item.status_code
		meta_finder_document.producer = metadata.get('Producer', '').rstrip('\x00')
		meta_finder_document.creator = metadata.get('Creator', '').rstrip('\x00')
		meta_finder_document.creation_date = metadata.get('CreationDate', '').rstrip('\x00')
		meta_finder_document.modified_date = metadata.get('ModDate', '').rstrip('\x00')
		meta_finder_document.author = metadata.get('Author', '').rstrip('\x00')
		meta_finder_document.title = metadata.get('Title', '').rstrip('\x00')
		meta_finder_document.os = metadata.get('OSInfo', '').rstrip('\x00')
		meta_finder_document.save()
