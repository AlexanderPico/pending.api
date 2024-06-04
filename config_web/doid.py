import copy

from biothings.web.settings.default import QUERY_KWARGS

ES_HOST = "http://localhost:9200"
ES_INDEX = "pending-doid"  # the index name convention is like "pending-<ApiName>"
ES_DOC_TYPE = "disease"

API_PREFIX = "doid"
API_VERSION = ""

_extra_kwargs = {"ignore_obsolete": {"type": bool, "default": True}}
QUERY_KWARGS = copy.deepcopy(QUERY_KWARGS)
QUERY_KWARGS["*"].update(_extra_kwargs)
ES_QUERY_BUILDER = "web.query_builders.OntologyQueryBuilder"
