# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.
import os
import sys
import shutil
from subprocess import call
import zipfile
import boto3
import botocore
import time
import json
import webbrowser
import hashlib
from botocore.exceptions import ClientError
from botocore.exceptions import ProfileNotFound

aws_profile_name = 'parlai_mturk'
region_name = 'us-west-2'

iam_role_name = 'parlai_relay_server'
lambda_function_name = 'parlai_relay_server'
lambda_permission_statement_id = 'lambda-permission-statement-id'
api_gateway_name = 'ParlaiRelayServer'
endpoint_api_name_html = 'html'  # For GET-ing HTML
endpoint_api_name_json = 'json'  # For GET-ing and POST-ing JSON

rds_db_instance_identifier = 'parlai-mturk-db'
rds_db_name = 'parlai_mturk_db'
rds_username = 'parlai_user'
rds_password = 'parlai_user_password'
rds_security_group_name = 'parlai-mturk-db-security-group'
rds_security_group_description = 'Security group for ParlAI MTurk DB'

parent_dir = os.path.dirname(os.path.abspath(__file__))
files_to_copy = [parent_dir+'/'+'data_model.py', parent_dir+'/'+'mturk_index.html']
lambda_server_directory_name = 'lambda_server'
lambda_server_zip_file_name = 'lambda_server.zip'
mturk_hit_frame_height = 650

def add_api_gateway_method(api_gateway_client, lambda_function_arn, rest_api_id, endpoint_resource, http_method_type, response_data_type):
    api_gateway_client.put_method(
        restApiId = rest_api_id,
        resourceId = endpoint_resource['id'],
        httpMethod = http_method_type,
        authorizationType = "NONE",
        apiKeyRequired = False,
    )

    response_parameters = { 'method.response.header.Access-Control-Allow-Origin': False }
    if response_data_type == 'html':
        response_parameters['method.response.header.Content-Type'] = False
    response_models = {}
    if response_data_type == 'json':
        response_models = { 'application/json': 'Empty' }
    api_gateway_client.put_method_response(
        restApiId = rest_api_id,
        resourceId = endpoint_resource['id'],
        httpMethod = http_method_type,
        statusCode = '200',
        responseParameters = response_parameters,
        responseModels = response_models
    )

    api_gateway_client.put_integration(
        restApiId = rest_api_id,
        resourceId = endpoint_resource['id'],
        httpMethod = http_method_type,
        type = 'AWS',
        integrationHttpMethod = 'POST', # this has to be POST
        uri = "arn:aws:apigateway:"+region_name+":lambda:path/2015-03-31/functions/"+lambda_function_arn+"/invocations",
        requestTemplates = {
            'application/json': \
'''{
  "body" : $input.json('$'),
  "headers": {
    #foreach($header in $input.params().header.keySet())
    "$header": "$util.escapeJavaScript($input.params().header.get($header))" #if($foreach.hasNext),#end

    #end
  },
  "method": "$context.httpMethod",
  "params": {
    #foreach($param in $input.params().path.keySet())
    "$param": "$util.escapeJavaScript($input.params().path.get($param))" #if($foreach.hasNext),#end

    #end
  },
  "query": {
    #foreach($queryParam in $input.params().querystring.keySet())
    "$queryParam": "$util.escapeJavaScript($input.params().querystring.get($queryParam))" #if($foreach.hasNext),#end

    #end
  }
}'''
        },
        passthroughBehavior = 'WHEN_NO_TEMPLATES'
    )

    response_parameters = { 'method.response.header.Access-Control-Allow-Origin': "'*'" }
    response_templates = { 'application/json': '' }
    if response_data_type == 'html':
        response_parameters['method.response.header.Content-Type'] = "'text/html'"
        response_templates = { "text/html": "$input.path('$')" }
    api_gateway_client.put_integration_response(
        restApiId = rest_api_id,
        resourceId = endpoint_resource['id'],
        httpMethod = http_method_type,
        statusCode = '200',
        responseParameters=response_parameters,
        responseTemplates=response_templates,
    )

def setup_aws_credentials():
    try:
        session = boto3.Session(profile_name=aws_profile_name)
    except ProfileNotFound as e:
        print('''AWS credentials not found. Please create an IAM user with programmatic access and AdministratorAccess policy at https://console.aws.amazon.com/iam/ (On the "Set permissions" page, choose "Attach existing policies directly" and then select "AdministratorAccess" policy). \nAfter creating the IAM user, please enter the user's Access Key ID and Secret Access Key below:''')
        aws_access_key_id = input('Access Key ID: ')
        aws_secret_access_key = input('Secret Access Key: ')
        if not os.path.exists(os.path.expanduser('~/.aws/')):
            os.makedirs(os.path.expanduser('~/.aws/'))
        aws_credentials_file_path = '~/.aws/credentials'
        aws_credentials_file_string = None
        if os.path.exists(os.path.expanduser(aws_credentials_file_path)):
            with open(os.path.expanduser(aws_credentials_file_path), 'r') as aws_credentials_file:
                aws_credentials_file_string = aws_credentials_file.read()
        with open(os.path.expanduser(aws_credentials_file_path), 'a+') as aws_credentials_file:
            if aws_credentials_file_string:
                if aws_credentials_file_string.endswith("\n\n"):
                    pass
                elif aws_credentials_file_string.endswith("\n"):
                    aws_credentials_file.write("\n")
                else:
                    aws_credentials_file.write("\n\n")
            aws_credentials_file.write("["+aws_profile_name+"]\n")
            aws_credentials_file.write("aws_access_key_id="+aws_access_key_id+"\n")
            aws_credentials_file.write("aws_secret_access_key="+aws_secret_access_key+"\n")
        print("AWS credentials successfully saved in "+aws_credentials_file_path+" file.\n")
    os.environ["AWS_PROFILE"] = aws_profile_name

def get_requester_key():
    # Compute requester key
    session = boto3.Session(profile_name=aws_profile_name)
    hash_gen = hashlib.sha512()
    hash_gen.update(session.get_credentials().access_key.encode('utf-8')+session.get_credentials().secret_key.encode('utf-8'))
    requester_key_gt = hash_gen.hexdigest()

    return requester_key_gt

def setup_rds():
    # Set up security group rules first
    ec2 = boto3.client('ec2', region_name=region_name)

    response = ec2.describe_vpcs()
    vpc_id = response.get('Vpcs', [{}])[0].get('VpcId', '')
    security_group_id = None

    try:
        response = ec2.create_security_group(GroupName=rds_security_group_name,
                                             Description=rds_security_group_description,
                                             VpcId=vpc_id)
        security_group_id = response['GroupId']
        print('RDS: Security group created.')
        
        data = ec2.authorize_security_group_ingress(
            GroupId=security_group_id,
            IpPermissions=[
                {
                 'IpProtocol': 'tcp',
                 'FromPort': 5432,
                 'ToPort': 5432,
                 'IpRanges': [{'CidrIp': '0.0.0.0/0'}],
                 'Ipv6Ranges': [{'CidrIpv6': '::/0'}]
                },
            ])
        print('RDS: Security group ingress IP permissions set.')
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.Duplicate':
            print('RDS: Security group already exists.')
            response = ec2.describe_security_groups(GroupNames=[rds_security_group_name])
            security_group_id = response['SecurityGroups'][0]['GroupId']

    rds = boto3.client('rds', region_name=region_name)
    try:
        rds.create_db_instance(DBInstanceIdentifier=rds_db_instance_identifier,
                               AllocatedStorage=20,
                               DBName=rds_db_name,
                               Engine='postgres',
                               # General purpose SSD
                               StorageType='gp2',
                               StorageEncrypted=False,
                               AutoMinorVersionUpgrade=True,
                               MultiAZ=False,
                               MasterUsername=rds_username,
                               MasterUserPassword=rds_password,
                               VpcSecurityGroupIds=[security_group_id],
                               DBInstanceClass='db.t2.micro',
                               Tags=[{'Key': 'Name', 'Value': rds_db_instance_identifier}])
        print('RDS: Starting RDS instance...')
    except ClientError as e:
        if e.response['Error']['Code'] == 'DBInstanceAlreadyExists':
            print('RDS: DB instance already exists.')
        else:
            raise

    response = rds.describe_db_instances(DBInstanceIdentifier=rds_db_instance_identifier)
    db_instances = response['DBInstances']
    db_instance = db_instances[0]
    status = db_instance['DBInstanceStatus']

    if status not in ['available', 'backing-up']:
        print("RDS: Waiting for newly created database to be available. This might take a couple minutes...")

    while status not in ['available', 'backing-up']:
        time.sleep(5)
        response = rds.describe_db_instances(DBInstanceIdentifier=rds_db_instance_identifier)
        db_instances = response['DBInstances']
        db_instance = db_instances[0]
        status = db_instance['DBInstanceStatus']
    
    endpoint = db_instance['Endpoint']
    host = endpoint['Address']

    print('RDS: DB instance ready.')

    return host

def setup_relay_server_api(mturk_submit_url, rds_host, task_config, is_sandbox, num_hits, requester_key_gt, should_clean_up_after_upload=True):
    # Dynamically generate handler.py file, and then create zip file
    print("Lambda: Preparing relay server code...")

    # Create clean folder for lambda server code
    if os.path.exists(parent_dir + '/' + lambda_server_directory_name):
        shutil.rmtree(parent_dir + '/' + lambda_server_directory_name)
    os.makedirs(parent_dir + '/' + lambda_server_directory_name)
    if os.path.exists(parent_dir + '/' + lambda_server_zip_file_name):
        os.remove(parent_dir + '/' + lambda_server_zip_file_name)

    # Copying files
    with open(parent_dir+'/handler_template.py', 'r') as handler_template_file:
        handler_file_string = handler_template_file.read()
    handler_file_string = handler_file_string.replace(
        '# {{block_task_config}}', 
        "frame_height = " + str(mturk_hit_frame_height) + "\n" + \
        "mturk_submit_url = \'" + mturk_submit_url + "\'\n" + \
        "rds_host = \'" + rds_host + "\'\n" + \
        "rds_db_name = \'" + rds_db_name + "\'\n" + \
        "rds_username = \'" + rds_username + "\'\n" + \
        "rds_password = \'" + rds_password + "\'\n" + \
        "requester_key_gt = \'" + requester_key_gt + "\'\n" + \
        "num_hits = " + str(num_hits) + "\n" + \
        "is_sandbox = " + str(is_sandbox) + "\n" + \
        'task_description = ' + task_config['task_description'])
    with open(parent_dir + '/' + lambda_server_directory_name+'/handler.py', "w") as handler_file:
        handler_file.write(handler_file_string)
    create_zip_file(
        lambda_server_directory_name=lambda_server_directory_name, 
        lambda_server_zip_file_name=lambda_server_zip_file_name,
        files_to_copy=files_to_copy
    )
    with open(parent_dir + '/' + lambda_server_zip_file_name, mode='rb') as zip_file:
        zip_file_content = zip_file.read()

    # Create Lambda function
    lambda_client = boto3.client('lambda', region_name=region_name)
    lambda_function_arn = None
    try: 
        # Case 1: if Lambda function exists
        lambda_function = lambda_client.get_function(FunctionName=lambda_function_name)
        print("Lambda: Function already exists. Uploading latest version of code...")
        lambda_function_arn = lambda_function['Configuration']['FunctionArn']
        # Upload latest code for Lambda function
        lambda_client.update_function_code(
            FunctionName = lambda_function_name,
            ZipFile = zip_file_content,
            Publish = True
        )
    except ClientError as e:
        # Case 2: if Lambda function does not exist
        print("Lambda: Function does not exist. Creating it...")
        iam_client = boto3.client('iam')
        try:
            iam_client.get_role(RoleName=iam_role_name)
        except ClientError as e:
            # Should create IAM role for Lambda server
            iam_client.create_role(
                RoleName = iam_role_name, 
                AssumeRolePolicyDocument = '''{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "Service": "lambda.amazonaws.com" }, "Action": "sts:AssumeRole" } ]}'''
            )
            iam_client.attach_role_policy(
                RoleName = iam_role_name,
                PolicyArn = 'arn:aws:iam::aws:policy/AWSLambdaFullAccess'
            )
            iam_client.attach_role_policy(
                RoleName = iam_role_name,
                PolicyArn = 'arn:aws:iam::aws:policy/AmazonRDSFullAccess'
            )
            iam_client.attach_role_policy(
                RoleName = iam_role_name,
                PolicyArn = 'arn:aws:iam::aws:policy/AmazonMechanicalTurkFullAccess'
            )

        iam = boto3.resource('iam')
        iam_role = iam.Role(iam_role_name)
        lambda_function_arn = None

        # Create the Lambda function and upload latest code
        while True:
            try:
                response = lambda_client.create_function(
                    FunctionName = lambda_function_name,
                    Runtime = 'python2.7',
                    Role = iam_role.arn,
                    Handler='handler.lambda_handler',
                    Code={
                        'ZipFile': zip_file_content
                    },
                    Timeout = 10, # in seconds
                    MemorySize = 128, # in MB
                    Publish = True,
                )
                lambda_function_arn = response['FunctionArn']
                break
            except ClientError as e:
                print("Lambda: Waiting for IAM role creation to take effect...")
                time.sleep(10)

        # Add permission to endpoints for calling Lambda function
        response = lambda_client.add_permission(
            FunctionName = lambda_function_name,
            StatementId = lambda_permission_statement_id,
            Action = 'lambda:InvokeFunction',
            Principal = 'apigateway.amazonaws.com',
        )

    # Clean up if needed
    if should_clean_up_after_upload:
        shutil.rmtree(parent_dir + '/' + lambda_server_directory_name)
        os.remove(parent_dir + '/' + lambda_server_zip_file_name)

    # Check API Gateway existence. 
    # If doesn't exist, create the APIs, point them to Lambda function, and set correct configurations
    api_gateway_exists = False
    rest_api_id = None
    api_gateway_client = boto3.client('apigateway', region_name=region_name)
    response = api_gateway_client.get_rest_apis()
    if not 'items' in response:
        api_gateway_exists = False
    else:
        rest_apis = response['items']
        for api in rest_apis:
            if api['name'] == api_gateway_name:
                api_gateway_exists = True
                rest_api_id = api['id']
                break
    if not api_gateway_exists:
        rest_api = api_gateway_client.create_rest_api(
            name = api_gateway_name,
        )
        rest_api_id = rest_api['id']

    # Create endpoint resources if doesn't exist
    html_endpoint_exists = False
    json_endpoint_exists = False
    root_endpoint_id = None
    response = api_gateway_client.get_resources(restApiId=rest_api_id)
    resources = response['items']
    for resource in resources:
        if resource['path'] == '/':
            root_endpoint_id = resource['id']
        elif resource['path'] == '/' + endpoint_api_name_html:
            html_endpoint_exists = True
        elif resource['path'] == '/' + endpoint_api_name_json:
            json_endpoint_exists = True

    if not html_endpoint_exists:
        print("API Gateway: Creating endpoint for html...")
        resource_for_html_endpoint = api_gateway_client.create_resource(
            restApiId = rest_api_id,
            parentId = root_endpoint_id,
            pathPart = endpoint_api_name_html
        )

        # Set up GET method
        add_api_gateway_method(
            api_gateway_client = api_gateway_client,
            lambda_function_arn = lambda_function_arn,
            rest_api_id = rest_api_id,
            endpoint_resource = resource_for_html_endpoint,
            http_method_type = 'GET',
            response_data_type = 'html'
        )
    else:
        print("API Gateway: Endpoint for html already exists.")

    if not json_endpoint_exists:
        print("API Gateway: Creating endpoint for json...")
        resource_for_json_endpoint = api_gateway_client.create_resource(
            restApiId = rest_api_id,
            parentId = root_endpoint_id,
            pathPart = endpoint_api_name_json
        )

        # Set up GET method
        add_api_gateway_method(
            api_gateway_client = api_gateway_client,
            lambda_function_arn = lambda_function_arn,
            rest_api_id = rest_api_id,
            endpoint_resource = resource_for_json_endpoint,
            http_method_type = 'GET',
            response_data_type = 'json'
        )

        # Set up POST method
        add_api_gateway_method(
            api_gateway_client = api_gateway_client,
            lambda_function_arn = lambda_function_arn,
            rest_api_id = rest_api_id,
            endpoint_resource = resource_for_json_endpoint,
            http_method_type = 'POST',
            response_data_type = 'json'
        )
    else:
        print("API Gateway: Endpoint for json already exists.")

    if not (html_endpoint_exists and json_endpoint_exists):
        api_gateway_client.create_deployment(
            restApiId = rest_api_id,
            stageName = "prod",
        )

    html_api_endpoint_url = 'https://' + rest_api_id + '.execute-api.' + region_name + '.amazonaws.com/prod/' + endpoint_api_name_html
    json_api_endpoint_url = 'https://' + rest_api_id + '.execute-api.' + region_name + '.amazonaws.com/prod/' + endpoint_api_name_json

    return html_api_endpoint_url, json_api_endpoint_url

def check_mturk_balance(num_hits, hit_reward, is_sandbox):
    client = boto3.client(
        service_name = 'mturk', 
        region_name = 'us-east-1',
        endpoint_url = 'https://mturk-requester-sandbox.us-east-1.amazonaws.com'
    )

    # Region is always us-east-1
    if not is_sandbox:
        client = boto3.client(service_name = 'mturk', region_name='us-east-1')

    # Test that you can connect to the API by checking your account balance
    # In Sandbox this always returns $10,000
    try:
        user_balance = float(client.get_account_balance()['AvailableBalance'])
    except ClientError as e:
        if e.response['Error']['Code'] == 'RequestError':
            print('ERROR: To use the MTurk API, you will need an Amazon Web Services (AWS) Account. Your AWS account must be linked to your Amazon Mechanical Turk Account. Visit https://requestersandbox.mturk.com/developer to get started. (Note: if you have recently linked your account, please wait for a couple minutes before trying again.)\n')
            quit()
        else:
            raise
    
    balance_needed = num_hits * hit_reward * 1.2

    if user_balance < balance_needed:
        print("You might not have enough money in your MTurk account. Please go to https://requester.mturk.com/account and increase your balance to at least $"+f'{balance_needed:.2f}'+", and then try again.")
        return False
    else:
        return True

def create_hit_type(hit_title, hit_description, hit_keywords, hit_reward, is_sandbox):
    client = boto3.client(
        service_name = 'mturk', 
        region_name = 'us-east-1',
        endpoint_url = 'https://mturk-requester-sandbox.us-east-1.amazonaws.com'
    )

    # Region is always us-east-1
    if not is_sandbox:
        client = boto3.client(service_name = 'mturk', region_name='us-east-1')

    # Create a qualification with Locale In('US', 'CA') requirement attached
    localRequirements = [{
        'QualificationTypeId': '00000000000000000071',
        'Comparator': 'In',
        'LocaleValues': [
            {'Country': 'US'}, 
            {'Country': 'CA'},
            {'Country': 'GB'},
            {'Country': 'AU'},
            {'Country': 'NZ'}
        ],
        'RequiredToPreview': True
    }]

    # Create the HIT type
    response = client.create_hit_type(
        AutoApprovalDelayInSeconds=4*7*24*3600, # auto-approve after 4 weeks
        AssignmentDurationInSeconds=1800,
        Reward=str(hit_reward),
        Title=hit_title,
        Keywords=hit_keywords,
        Description=hit_description,
        QualificationRequirements=localRequirements
    )
    hit_type_id = response['HITTypeId']
    return hit_type_id

def create_hit_with_hit_type(page_url, hit_type_id, is_sandbox):
    page_url = page_url.replace('&', '&amp;')

    question_data_struture = '''<ExternalQuestion xmlns="http://mechanicalturk.amazonaws.com/AWSMechanicalTurkDataSchemas/2006-07-14/ExternalQuestion.xsd">
      <ExternalURL>'''+page_url+'''</ExternalURL>
      <FrameHeight>'''+str(mturk_hit_frame_height)+'''</FrameHeight>
    </ExternalQuestion>
    '''

    client = boto3.client(
        service_name = 'mturk', 
        region_name = 'us-east-1',
        endpoint_url = 'https://mturk-requester-sandbox.us-east-1.amazonaws.com'
    )

    # Region is always us-east-1
    if not is_sandbox:
        client = boto3.client(service_name = 'mturk', region_name='us-east-1')

    # Create the HIT 
    response = client.create_hit_with_hit_type(
        HITTypeId=hit_type_id,
        MaxAssignments=1,
        LifetimeInSeconds=31536000,
        Question=question_data_struture,
        # AssignmentReviewPolicy={
        #     'PolicyName': 'string',
        #     'Parameters': [
        #         {
        #             'Key': 'string',
        #             'Values': [
        #                 'string',
        #             ],
        #             'MapEntries': [
        #                 {
        #                     'Key': 'string',
        #                     'Values': [
        #                         'string',
        #                     ]
        #                 },
        #             ]
        #         },
        #     ]
        # },
        # HITReviewPolicy={
        #     'PolicyName': 'string',
        #     'Parameters': [
        #         {
        #             'Key': 'string',
        #             'Values': [
        #                 'string',
        #             ],
        #             'MapEntries': [
        #                 {
        #                     'Key': 'string',
        #                     'Values': [
        #                         'string',
        #                     ]
        #                 },
        #             ]
        #         },
        #     ]
        # },
    )

    # response = client.create_hit(
    #     MaxAssignments = 1,
    #     LifetimeInSeconds = 31536000,
    #     AssignmentDurationInSeconds = 1800,
    #     Reward = str(hit_reward),
    #     Title = hit_title,
    #     Keywords = hit_keywords,
    #     Description = hit_description,
    #     Question = question_data_struture,
    #     #QualificationRequirements = localRequirements
    # )

    # The response included several fields that will be helpful later
    hit_type_id = response['HIT']['HITTypeId']
    hit_id = response['HIT']['HITId']
    hit_link = "https://workersandbox.mturk.com/mturk/preview?groupId=" + hit_type_id
    if not is_sandbox:
        hit_link = "https://www.mturk.com/mturk/preview?groupId=" + hit_type_id
    return hit_link

def setup_all_dependencies(lambda_server_directory_name):
    devnull = open(os.devnull, 'w')
    parent_dir = os.path.dirname(os.path.abspath(__file__))

    # Set up all other dependencies
    command_str = "pip install --target="+parent_dir+'/'+lambda_server_directory_name+" -r "+parent_dir+"/lambda_requirements.txt"
    command = command_str.split(" ")
    call(command, stdout=devnull, stderr=devnull)

    # Set up psycopg2
    command = "git clone https://github.com/yf225/awslambda-psycopg2.git".split(" ")
    call(command, stdout=devnull, stderr=devnull)
    shutil.copytree("./awslambda-psycopg2/with_ssl_support/psycopg2", parent_dir+'/'+lambda_server_directory_name+"/psycopg2")
    shutil.rmtree("./awslambda-psycopg2")

def create_zip_file(lambda_server_directory_name, lambda_server_zip_file_name, files_to_copy=None, verbose=False):
    setup_all_dependencies(lambda_server_directory_name)
    parent_dir = os.path.dirname(os.path.abspath(__file__))

    src = parent_dir + '/' + lambda_server_directory_name
    dst = parent_dir + '/' + lambda_server_zip_file_name

    if files_to_copy:
        for file_path in files_to_copy:
            shutil.copy2(file_path, src)

    zf = zipfile.ZipFile("%s" % (dst), "w", zipfile.ZIP_DEFLATED)
    abs_src = os.path.abspath(src)
    for dirname, subdirs, files in os.walk(src):
        for filename in files:
            absname = os.path.abspath(os.path.join(dirname, filename))
            os.chmod(absname, 0o777)
            arcname = os.path.relpath(absname, abs_src)
            if verbose:
                print('zipping %s as %s' % (os.path.join(dirname, filename),
                                            arcname))
            zf.write(absname, arcname)
    zf.close()

    if verbose:
        print("Done!")

def setup_aws(task_config, num_hits, is_sandbox):
    mturk_submit_url = 'https://workersandbox.mturk.com/mturk/externalSubmit'
    if not is_sandbox:
        mturk_submit_url = 'https://www.mturk.com/mturk/externalSubmit'
    requester_key_gt = get_requester_key()
    rds_host = setup_rds()
    html_api_endpoint_url, json_api_endpoint_url = setup_relay_server_api(mturk_submit_url, rds_host, task_config, is_sandbox, num_hits, requester_key_gt)

    return html_api_endpoint_url, json_api_endpoint_url, requester_key_gt

def clean_aws():
    setup_aws_credentials()

    # Remove RDS database
    try:
        rds = boto3.client('rds', region_name=region_name)
        response = rds.delete_db_instance(
            DBInstanceIdentifier=rds_db_instance_identifier,
            SkipFinalSnapshot=True,
        )
        response = rds.describe_db_instances(DBInstanceIdentifier=rds_db_instance_identifier)
        db_instances = response['DBInstances']
        db_instance = db_instances[0]
        status = db_instance['DBInstanceStatus']

        if status == 'deleting':
            print("RDS: Deleting database. This might take a couple minutes...")

        try:
            while status == 'deleting':
                time.sleep(5)
                response = rds.describe_db_instances(DBInstanceIdentifier=rds_db_instance_identifier)
                db_instances = response['DBInstances']
                db_instance = db_instances[0]
                status = db_instance['DBInstanceStatus']
        except ClientError as e:
            print("RDS: Database deleted.")

    except ClientError as e:
        print("RDS: Database doesn't exist.")

    # Remove RDS security group
    try:
        ec2 = boto3.client('ec2', region_name=region_name)

        response = ec2.describe_security_groups(GroupNames=[rds_security_group_name])
        security_group_id = response['SecurityGroups'][0]['GroupId']

        response = ec2.delete_security_group(
            DryRun=False,
            GroupName=rds_security_group_name,
            GroupId=security_group_id
        )
        print("RDS: Security group removed.")
    except ClientError as e:
        print("RDS: Security group doesn't exist.")

    # Remove API Gateway endpoints
    api_gateway_client = boto3.client('apigateway', region_name=region_name)
    api_gateway_exists = False
    rest_api_id = None
    response = api_gateway_client.get_rest_apis()
    if not 'items' in response:
        api_gateway_exists = False
    else:
        rest_apis = response['items']
        for api in rest_apis:
            if api['name'] == api_gateway_name:
                api_gateway_exists = True
                rest_api_id = api['id']
                break
    if api_gateway_exists:
        response = api_gateway_client.delete_rest_api(
            restApiId=rest_api_id
        )
        print("API Gateway: Endpoints are removed.")
    else:
        print("API Gateway: Endpoints don't exist.")

    # Remove permission for calling Lambda function
    try:
        lambda_client = boto3.client('lambda', region_name=region_name)
        response = lambda_client.remove_permission(
            FunctionName=lambda_function_name,
            StatementId=lambda_permission_statement_id
        )
        print("Lambda: Permission removed.")
    except ClientError as e:
        print("Lambda: Permission doesn't exist.")

    # Remove Lambda function
    try:
        lambda_client = boto3.client('lambda', region_name=region_name)
        response = lambda_client.delete_function(
            FunctionName=lambda_function_name
        )
        print("Lambda: Function removed.")
    except ClientError as e:
        print("Lambda: Function doesn't exist.")

    # Remove IAM role
    try:
        iam_client = boto3.client('iam')

        try:
            response = iam_client.detach_role_policy(
                RoleName=iam_role_name,
                PolicyArn='arn:aws:iam::aws:policy/AWSLambdaFullAccess'
            )
        except ClientError as e:
            pass

        try:
            response = iam_client.detach_role_policy(
                RoleName=iam_role_name,
                PolicyArn='arn:aws:iam::aws:policy/AmazonRDSFullAccess'
            )
        except ClientError as e:
            pass

        try:
            response = iam_client.detach_role_policy(
                RoleName=iam_role_name,
                PolicyArn='arn:aws:iam::aws:policy/AmazonMechanicalTurkFullAccess'
            )
        except ClientError as e:
            pass

        response = iam_client.delete_role(
            RoleName=iam_role_name
        )
        time.sleep(10)
        print("IAM: Role removed.")
    except ClientError as e:
        print("IAM: Role doesn't exist.")

if __name__ == "__main__":
    if sys.argv[1] == 'clean':
        clean_aws()