from aws_cdk import core as cdk
from aws_cdk import core
import aws_cdk.aws_ec2 as ec2 
import aws_cdk.aws_eks as eks
import aws_cdk.aws_iam as iam
import aws_cdk.aws_rds as rds
import aws_cdk.aws_secretsmanager as sm
import aws_cdk.aws_route53 as route53 

import yaml, requests, os, sys

class InfraStack(cdk.Stack):

    def __init__(self, scope: cdk.Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ####################
        # Load ENV VARS
        ####################
        
        try:
            document = open('values.yaml', 'r')
            parms = yaml.safe_load_all(document).__next__()
        except:
            print('Parameter file is required! Error:', sys.exc_info()[0])
            os._exit(1)

        p_secret_arn = parms.get('secretsManager').get('arn')
        if p_secret_arn == None:
            print('Secrets ARN is required in parameter file! Error:', sys.exc_info()[0])
            os._exit(1)

        p_namespace = 'default' if parms.get('namespace') == None else parms.get('namespace')

        p_workers_type = 't3a.medium'
        p_workers_number = 2

        if parms.get('workers') != None:
            p_workers_type = parms.get('workers').get('instanceType')
            p_workers_number = parms.get('workers').get('number')

        ####################
        # VPC
        ####################

        vpc = ec2.Vpc(self, 'vpc',
                      max_azs=2,
                      nat_gateway_provider=ec2.NatProvider.instance(instance_type=ec2.InstanceType('t3a.micro')))

        ####################
        # Kubernetes Cluster
        ####################

        # EKS Cluster
        cluster = eks.Cluster(self, 'rapyd-eks',
            version=eks.KubernetesVersion.V1_20,
            cluster_name='rapyd-eks',
            default_capacity=p_workers_number,
            default_capacity_instance=ec2.InstanceType(p_workers_type),
            vpc=vpc
        )

        # Namespace Creation
        if p_namespace != None:
            doc = {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata" : {
                    "name": p_namespace
                }        
            }

            namespace_r = cluster.add_manifest('namespaceR', doc)

        # Add service account
        sa = cluster.add_service_account('rapydSecretSA', name='rapyd-secret-sa', namespace=p_namespace)

        sa.add_to_principal_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
                                    actions=['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
                                    resources=[p_secret_arn]))

        if p_namespace != None:
            sa.node.add_dependency(namespace_r)

        # CSI Chart
        csi_chart = cluster.add_helm_chart('csiSecretsStoreChart',
                                            release='csi-secrets-store',
                                            chart='secrets-store-csi-driver',
                                            repository='https://raw.githubusercontent.com/kubernetes-sigs/secrets-store-csi-driver/master/charts',
                                            namespace='kube-system')

        # Installing the AWS Provider
        manifest = yaml.safe_load_all(requests.get('https://raw.githubusercontent.com/aws/secrets-store-csi-driver-provider-aws/main/deployment/aws-provider-installer.yaml').text)
        for i, doc in enumerate(manifest):
            resource = cluster.add_manifest('ascpResource' + str(i), doc)
            resource.node.add_dependency(csi_chart)
            resource.node.add_dependency(sa)

            if doc['kind'] == 'DaemonSet':
                aws_csi = resource
        
        ####################
        # MYSQL Cluster
        ####################
        
        # Read Secret
        secret_rapyd = sm.Secret.from_secret_arn(self, 'secretsRapydbot', p_secret_arn)

        # DB Security group
        sg_aurora = ec2.SecurityGroup(self, 'sgAurora', vpc=vpc, security_group_name= 'AuroraMysql')
        sg_aurora.add_ingress_rule(cluster.cluster_security_group, ec2.Port.tcp(3306))

        # Create cluster
        dbCluster = rds.DatabaseCluster(self, 'database',
                                        engine=rds.DatabaseClusterEngine.aurora_mysql(version=rds.AuroraMysqlEngineVersion.VER_2_09_2),
                                        credentials=rds.Credentials.from_secret(secret_rapyd),
                                        cluster_identifier='db-cluster',
                                        instances=1,
                                        instance_props=rds.InstanceProps(
                                            vpc=vpc,
                                            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE),
                                            instance_type=ec2.InstanceType('t3.small'),
                                            security_groups=[sg_aurora]
                                        ))

        ####################
        # Privete DNS Zone
        ####################

        # Create dns zone
        zone_pv = route53.PrivateHostedZone(self, 'privateZone',
                                            vpc=vpc,
                                            zone_name='rapydbot.local')

        # DB Write Record
        db_record = route53.CnameRecord(self, 'dbWriteRecord',
                                        domain_name=dbCluster.cluster_endpoint.hostname,
                                        record_name='db',
                                        zone=zone_pv,
                                        ttl=core.Duration.minutes(1))

        # DB Read Record
        route53.CnameRecord(self, 'dbReadRecord',
                            domain_name=dbCluster.cluster_read_endpoint.hostname,
                            record_name='db-ro',
                            zone=zone_pv,
                            ttl=core.Duration.minutes(1))

        ####################
        # Deploy APP
        ####################

        if parms.get('secretsManager').get('secretName') == None:
            parms['secretsManager']['secretName'] = secret_rapyd.secret_name

        app_chart = cluster.add_helm_chart('rapydbotChart',
                                            release='my-bot',
                                            chart='rapydbot',
                                            values=parms,
                                            repository='https://aufacicenta.github.io/rapydbot-chart/',
                                            version=None if parms.get('charVersion') == None else parms.get('charVersion'),
                                            namespace=p_namespace)
        
        app_chart.node.add_dependency(dbCluster)
        app_chart.node.add_dependency(aws_csi)

        ###############################
        # OutPuts
        ###############################

        bot_service_address = eks.KubernetesObjectValue(self, 'botServiceLB',
                                                        cluster=cluster,
                                                        object_type='service',
                                                        object_name='my-bot-bot-service',
                                                        json_path='.status.loadBalancer.ingress[0].hostname', 
                                                        object_namespace=p_namespace)

        bot_service_address.node.add_dependency(app_chart)

        core.CfnOutput(
            self, 'BOT_SERVICE_ENDPOINT',
            value=bot_service_address.value)
        