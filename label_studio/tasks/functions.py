import json
import logging
import os
import sys

from core.models import AsyncMigrationStatus
from core.redis import start_job_async_or_sync
from core.utils.common import batch
from data_export.mixins import ExportMixin
from data_export.models import DataExport
from data_export.serializers import ExportDataSerializer
from django.conf import settings
from organizations.models import Organization
from projects.models import Project
from tasks.models import Annotation, Prediction, Task

logger = logging.getLogger(__name__)


def calculate_stats_all_orgs(from_scratch, redis, migration_name='0018_manual_migrate_counters'):
    logger = logging.getLogger(__name__)
    # Don't load full Organization objects bc some columns (contact_info, verify_ssl_certs)
    # aren't created until after a migration calls this code
    organization_ids = Organization.objects.order_by('-id').values_list('id', flat=True)

    for org_id in organization_ids:
        logger.debug(f'Start recalculating stats for Organization {org_id}')

        # start async calculation job on redis
        start_job_async_or_sync(
            redis_job_for_calculation,
            org_id,
            from_scratch,
            redis=redis,
            queue_name='critical',
            job_timeout=3600 * 24,  # 24 hours for one organization
            migration_name=migration_name,
        )

        logger.debug(f'Organization {org_id} stats were recalculated')

    logger.debug('All organizations were recalculated')


def redis_job_for_calculation(org_id, from_scratch, migration_name='0018_manual_migrate_counters'):
    """
    Recalculate counters for projects list
    :param org_id: ID of organization to recalculate
    :param from_scratch: Start calculation from scratch or skip calculated tasks
    """
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    projects = Project.objects.filter(organization_id=org_id).order_by('-updated_at')
    for project in projects:
        migration = AsyncMigrationStatus.objects.create(
            project=project,
            name=migration_name,
            status=AsyncMigrationStatus.STATUS_STARTED,
        )
        logger.debug(
            f'Start processing stats project <{project.title}> ({project.id}) '
            f'with task count {project.tasks.count()} and updated_at {project.updated_at}'
        )

        task_count = project.update_tasks_counters(project.tasks.all(), from_scratch=from_scratch)

        migration.status = AsyncMigrationStatus.STATUS_FINISHED
        migration.meta = {'tasks_processed': task_count, 'total_project_tasks': project.tasks.count()}
        migration.save()
        logger.debug(
            f'End processing counters for project <{project.title}> ({project.id}), '
            f'processed {str(task_count)} tasks'
        )


def export_project(project_id, export_format, path, serializer_context=None):
    logger = logging.getLogger(__name__)

    project = Project.objects.get(id=project_id)

    export_format = export_format.upper()
    supported_formats = [s['name'] for s in DataExport.get_export_formats(project)]
    assert export_format in supported_formats, f'Export format is not supported, please use {supported_formats}'

    task_ids = (
        Task.objects.filter(project=project).select_related('project').prefetch_related('annotations', 'predictions')
    )

    logger.debug(f'Start exporting project <{project.title}> ({project.id}) with task count {task_ids.count()}.')

    # serializer context
    if isinstance(serializer_context, str):
        serializer_context = json.loads(serializer_context)
    serializer_options = ExportMixin._get_export_serializer_option(serializer_context)

    # export cycle
    tasks = []
    for _task_ids in batch(task_ids, 1000):
        tasks += ExportDataSerializer(_task_ids, many=True, **serializer_options).data

    # convert to output format
    export_stream, _, filename = DataExport.generate_export_file(
        project, tasks, export_format, settings.CONVERTER_DOWNLOAD_RESOURCES, {}
    )

    # write to file
    filepath = os.path.join(path, filename) if os.path.isdir(path) else path
    with open(filepath, 'wb') as file:
        file.write(export_stream.read())

    logger.debug(f'End exporting project <{project.title}> ({project.id}) in {export_format} format.')

    return filepath


def _fill_annotations_project(project_id):
    Annotation.objects.filter(task__project_id=project_id).update(project_id=project_id)


def fill_annotations_project():
    logger.info('Start filling project field for Annotation model')

    project_ids = Project.objects.all().values_list('id', flat=True)
    for project_id in project_ids:
        start_job_async_or_sync(_fill_annotations_project, project_id)

    logger.info('Finished filling project field for Annotation model')


def _fill_predictions_project(migration_name='0043_auto_20230825'):
    project_ids = Project.objects.all().values_list('id', flat=True)
    for project_id in project_ids:
        migration = AsyncMigrationStatus.objects.create(
            project_id=project_id,
            name=migration_name,
            status=AsyncMigrationStatus.STATUS_STARTED,
        )

        updated_count = Prediction.objects.filter(task__project_id=project_id).update(project_id=project_id)

        migration.status = AsyncMigrationStatus.STATUS_FINISHED
        migration.meta = {
            'predictions_processed': updated_count,
            'total_project_predictions': Prediction.objects.filter(project_id=project_id).count(),
        }
        migration.save()


def fill_predictions_project(migration_name):
    logger.info('Start filling project field for Prediction model')
    start_job_async_or_sync(_fill_predictions_project, migration_name=migration_name)
    logger.info('Finished filling project field for Prediction model')
