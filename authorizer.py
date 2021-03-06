import re
import os
import json
import jose
import time
import requests
from http import HTTPStatus
from jose import jwk, jwt
from jose.utils import base64url_decode


def lambda_handler(event, context):
    region = os.environ['AWS_DEFAULT_REGION']
    user_pool_id = os.environ['USER_POOL_ID']
    client_id = os.environ['CLIENT_ID']
    account_id = event['requestContext']['accountId']
    api_id = event['requestContext']['apiId']
    stage = event['requestContext']['stage']
    print(f'region:{region}, account_id:{account_id}, api_id:{api_id}, stage:{stage}, user_pool_id:{user_pool_id}')

    policy = AuthPolicy('', account_id)
    policy.region = region
    policy.restApiId = api_id
    policy.stage = stage

    try:
        # Validate IdToken
        token = event['headers']['Authorization']
        user_agent = event['headers']['User-Agent']
        validate_token(token, region, user_pool_id, client_id, user_agent)

        # Allow user to call APIGateway.
        policy.allowAllMethods()
        response = policy.build()
        print(response)
        return response
    except InvalidTokenError as e:
        policy.denyAllMethods()
        response = policy.build()
        response['context'] = {'message': e}
        print(response)
        return response
    except Exception as e:
        raise e


def validate_token(token: str, region: str, user_pool_id: str, client_id: str, user_agent: str):
    # Validate whether to match local public key and remote one.
    keys_url = f'https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json'
    headers = jwt.get_unverified_headers(token)
    print(f'token:{token}')

    response = requests.get(keys_url)
    keys = json.loads(response.text)['keys']
    target_key = None
    for key in keys:
        if key['kid'] == headers['kid']:
            target_key = key
    if target_key is None:
        raise InvalidTokenError('Invalid public key in headers.')

    # Validate signature of JWT.
    public_key = jwk.construct(target_key)
    print(f'public_key:{public_key}')
    message = token.rsplit('.', 1)[0].encode('utf-8')  # message = header + payload
    signature = token.rsplit('.', 1)[1].encode('utf-8')
    decode_signature = base64url_decode(signature)
    print(f'message:{message}')
    print(f'signature:{signature}')
    print(f'decode_signature:{decode_signature}')

    if not public_key.verify(message, decode_signature):
        raise InvalidTokenError('Invalid token signature.')

    # Validate expire of JWT.
    claims = jwt.get_unverified_claims(token)
    if time.time() > claims['exp']:
        raise InvalidTokenError('Token is expired.')

    # Validate aud claim which includes Client ID in Cognito.
    if claims['aud'] != client_id:
        raise InvalidTokenError('Invalid aud(Cognito Client ID).')

    # Validate UserAgent in header. This is allowed cognito-authorizer only.
    if user_agent != 'cognito-authorizer':
        raise InvalidTokenError('Invalid UserAgent.')


class InvalidTokenError(Exception):
    pass


class HttpVerb:
    GET = 'GET'
    POST = 'POST'
    PUT = 'PUT'
    PATCH = 'PATCH'
    HEAD = 'HEAD'
    DELETE = 'DELETE'
    OPTIONS = 'OPTIONS'
    ALL = '*'


class AuthPolicy(object):
    # The AWS account id the policy will be generated for. This is used to create the method ARNs.
    awsAccountId = ''
    # The principal used for the policy, this should be a unique identifier for the end user.
    principalId = ''
    # The policy version used for the evaluation. This should always be '2012-10-17'
    version = '2012-10-17'
    # The regular expression used to validate resource paths for the policy
    pathRegex = '^[/.a-zA-Z0-9-\*]+$'

    '''Internal lists of allowed and denied methods.

    These are lists of objects and each object has 2 properties: A resource
    ARN and a nullable conditions statement. The build method processes these
    lists and generates the approriate statements for the final policy.
    '''
    allowMethods = []
    denyMethods = []

    # The API Gateway API id. By default this is set to '*'
    restApiId = '*'
    # The region where the API is deployed. By default this is set to '*'
    region = '*'
    # The name of the stage used in the policy. By default this is set to '*'
    stage = '*'

    def __init__(self, principal, awsAccountId):
        self.awsAccountId = awsAccountId
        self.principalId = principal
        self.allowMethods = []
        self.denyMethods = []

    def _addMethod(self, effect, verb, resource, conditions):
        '''Adds a method to the internal lists of allowed or denied methods. Each object in
        the internal list contains a resource ARN and a condition statement. The condition
        statement can be null.'''
        if verb != '*' and not hasattr(HttpVerb, verb):
            raise NameError('Invalid HTTP verb ' + verb + '. Allowed verbs in HttpVerb class')
        resourcePattern = re.compile(self.pathRegex)
        if not resourcePattern.match(resource):
            raise NameError('Invalid resource path: ' + resource + '. Path should match ' + self.pathRegex)

        if resource[:1] == '/':
            resource = resource[1:]

        resourceArn = 'arn:aws:execute-api:{}:{}:{}/{}/{}/{}'.format(self.region, self.awsAccountId, self.restApiId, self.stage, verb, resource)

        if effect.lower() == 'allow':
            self.allowMethods.append({
                'resourceArn': resourceArn,
                'conditions': conditions
            })
        elif effect.lower() == 'deny':
            self.denyMethods.append({
                'resourceArn': resourceArn,
                'conditions': conditions
            })

    def _getEmptyStatement(self, effect):
        '''Returns an empty statement object prepopulated with the correct action and the
        desired effect.'''
        statement = {
            'Action': 'execute-api:Invoke',
            'Effect': effect[:1].upper() + effect[1:].lower(),
            'Resource': []
        }

        return statement

    def _getStatementForEffect(self, effect, methods):
        '''This function loops over an array of objects containing a resourceArn and
        conditions statement and generates the array of statements for the policy.'''
        statements = []

        if len(methods) > 0:
            statement = self._getEmptyStatement(effect)

            for curMethod in methods:
                if curMethod['conditions'] is None or len(curMethod['conditions']) == 0:
                    statement['Resource'].append(curMethod['resourceArn'])
                else:
                    conditionalStatement = self._getEmptyStatement(effect)
                    conditionalStatement['Resource'].append(curMethod['resourceArn'])
                    conditionalStatement['Condition'] = curMethod['conditions']
                    statements.append(conditionalStatement)

            if statement['Resource']:
                statements.append(statement)

        return statements

    def allowAllMethods(self):
        '''Adds a '*' allow to the policy to authorize access to all methods of an API'''
        self._addMethod('Allow', HttpVerb.ALL, '*', [])

    def denyAllMethods(self):
        '''Adds a '*' allow to the policy to deny access to all methods of an API'''
        self._addMethod('Deny', HttpVerb.ALL, '*', [])

    def allowMethod(self, verb, resource):
        '''Adds an API Gateway method (Http verb + Resource path) to the list of allowed
        methods for the policy'''
        self._addMethod('Allow', verb, resource, [])

    def denyMethod(self, verb, resource):
        '''Adds an API Gateway method (Http verb + Resource path) to the list of denied
        methods for the policy'''
        self._addMethod('Deny', verb, resource, [])

    def allowMethodWithConditions(self, verb, resource, conditions):
        '''Adds an API Gateway method (Http verb + Resource path) to the list of allowed
        methods and includes a condition for the policy statement. More on AWS policy
        conditions here: http://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements.html#Condition'''
        self._addMethod('Allow', verb, resource, conditions)

    def denyMethodWithConditions(self, verb, resource, conditions):
        '''Adds an API Gateway method (Http verb + Resource path) to the list of denied
        methods and includes a condition for the policy statement. More on AWS policy
        conditions here: http://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements.html#Condition'''
        self._addMethod('Deny', verb, resource, conditions)

    def build(self):
        '''Generates the policy document based on the internal lists of allowed and denied
        conditions. This will generate a policy with two main statements for the effect:
        one statement for Allow and one statement for Deny.
        Methods that includes conditions will have their own statement in the policy.'''
        if ((self.allowMethods is None or len(self.allowMethods) == 0) and
                (self.denyMethods is None or len(self.denyMethods) == 0)):
            raise NameError('No statements defined for the policy')

        policy = {
            'principalId': self.principalId,
            'policyDocument': {
                'Version': self.version,
                'Statement': []
            }
        }

        policy['policyDocument']['Statement'].extend(self._getStatementForEffect('Allow', self.allowMethods))
        policy['policyDocument']['Statement'].extend(self._getStatementForEffect('Deny', self.denyMethods))

        return policy
