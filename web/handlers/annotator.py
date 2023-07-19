"""
Translator Node Annotator Service Handler
"""
import inspect
import logging

import biothings_client
from biothings.utils.common import get_dotfield_value
from biothings.web.handlers import BaseAPIHandler
from tornado.web import HTTPError

logger = logging.getLogger(__name__)

BIOLINK_PREFIX_to_BioThings = {
    "NCBIGene": {"type": "gene", "field": "entrezgene"},
    "ENSEMBL": {"type": "gene", "field": "ensembl.gene"},
    "UniProtKB": {"type": "gene", "field": "uniprot.Swiss-Prot"},
    "INCHIKEY": {"type": "chem"},
    "CHEMBL.COMPOUND": {
        "type": "chem",
        "field": "chembl.molecule_chembl_id",
        # "converter": lambda x: x.replace("CHEMBL.COMPOUND:", "CHEMBL"),
    },
    "PUBCHEM.COMPOUND": {"type": "chem", "field": "pubchem.cid"},
    "CHEBI": {"type": "chem", "field": "chebi.id", "keep_prefix": True},
    "UNII": {"type": "chem", "field": "unii.unii"},
    "MONDO": {"type": "disease", "field": "mondo.mondo", "keep_prefix": True},
    "DOID": {"type": "disease", "field": "disease_ontology.doid", "keep_prefix": True},
}

# ANNOTAION_FIELD_TRANSFORMATION = {
#     "chembl.drug_indications.mesh_id": lambda x: append_prefix(x, "MESH"),
# }


class ResponseTransformer:
    def _transform_chembl_drug_indications(self, res):
        def _append_mesh_prefix(chembl):
            xli = chembl.get("drug_indications", [])
            for doc in xli:
                if "mesh_id" in doc:
                    # Add MESH prefix to chembl.drug_indications.mesh_id field
                    doc["mesh_id"] = append_prefix(doc["mesh_id"], "MESH")

        chembl = res.get("chembl", {})
        if chembl:
            if isinstance(chembl, list):
                # in case returned chembl is a list, rare but still possible
                for c in chembl:
                    _append_mesh_prefix(c)
            else:
                _append_mesh_prefix(chembl)

        return res

    def transform(self, res):
        """transform the response from biothings client"""
        for fn_name, fn in inspect.getmembers(self, predicate=inspect.ismethod):
            if fn_name.startswith("_transform_"):
                if isinstance(res, list):
                    res = [fn(r) for r in res]
                else:
                    res = fn(res)
        return res


class TRAPIInputError(ValueError):
    pass


class InvalidCurieError(ValueError):
    pass


def list2dict(li, key):
    out = {}
    for d in li:
        k = d[key]
        if k not in out:
            out[k] = [d]
        else:
            out[k].append(d)
    return out


def append_prefix(id, prefix):
    """append prefix to id if not already present to make it a valid Curie ID
    Note that prefix parameter should not include the trailing colon
    """
    return f"{prefix}:{id}" if not id.startswith(prefix) else id


class Annotator:
    annotator_clients = {
        "gene": {
            "client": biothings_client.get_client("gene"),
            "fields": ["name", "symbol", "summary", "type_of_gene", "MIM", "HGNC", "MGI", "RGD", "alias", "interpro"],
            "scopes": ["entrezgene", "ensemblgene", "uniprot", "accession", "retired"],
        },
        "chem": {
            "client": biothings_client.get_client("chem"),
            "fields": [
                # IDs
                "pubchem.cid",
                "pubchem.inchikey",
                "chembl.molecule_chembl_id",
                "drugbank.id",
                "chebi.id",
                "unii.unii",
                # "chembl.unii",
                # Names
                "chebi.name",
                "chembl.pref_name",
                # Structure
                "chebi.iupac",
                "chembl.smiles",
                "pubchem.inchi",
                "pubchem.molecular_formula",
                "pubchem.molecular_weight",
                # chemical types
                "chembl.molecule_type",
                "chembl.structure_type",
                # chebi roles etc
                "chebi.relationship",
                # drug info
                "unichem.rxnorm",  # drug name
                "pharmgkb.trade_names",  # drug name
                "chembl.drug_indications",
                "aeolus.indications",
                "chembl.drug_mechanisms",
                "chembl.atc_classifications",
                "chembl.max_phase",
                "chembl.first_approval",
                "drugcentral.approval",
                "chembl.first_in_class",
                "chembl.inorganic_flag",
                "chembl.prodrug",
                "chembl.therapeutic_flag",
                "cheml.withdrawn_flag",
                "drugcentral.drug_dosage",
                "ndc.routename",
                "ndc.producttypename",
                "ndc.pharm_classes",
            ],
            "scopes": ["_id", "chebi.id", "chembl.molecule_chembl_id", "pubchem.cid", "drugbank.id", "unii.unii"],
        },
        "disease": {
            "client": biothings_client.get_client("disease"),
            "fields": [
                # IDs
                "disease_ontology.doid" "mondo.mondo",
                "umls.umls",
                # Names
                "disease_ontology.name",
                "mondo.label"
                # Description
                "mondo.definition",
                "disease_ontology.def",
                # Xrefs
                "mondo.xrefs",
                "disease_ontology.xrefs",
                # Synonyms
                "mondo.synonym",
                "disease_ontology.synonyms",
            ],
            "scopes": ["mondo.mondo", "disease_ontology.doid", "umls.umls"],
        },
    }

    def parse_curie(self, curie, return_type=True, return_id=True):
        """return a both type and if (as a tuple) or either based on the input curie"""
        if ":" not in curie:
            raise InvalidCurieError(f"Invalid input curie id: {curie}")
        _prefix, _id = curie.split(":", 1)
        _type = BIOLINK_PREFIX_to_BioThings.get(_prefix, {}).get("type", None)
        if return_id:
            if not _type or BIOLINK_PREFIX_to_BioThings[_prefix].get("keep_prefix", False):
                _id = curie
            cvtr = BIOLINK_PREFIX_to_BioThings.get(_prefix, {}).get("converter", None)
            if cvtr:
                _id = cvtr(curie)
        if return_type and return_id:
            return _type, _id
        elif return_type:
            return _type
        elif return_id:
            return _id

    def query_biothings(self, node_type, query_list, fields=None):
        """Query biothings client based on node_type for a list of ids"""
        client = self.annotator_clients[node_type]["client"]
        fields = fields or self.annotator_clients[node_type]["fields"]
        scopes = self.annotator_clients[node_type]["scopes"]
        logger.info("Querying annotations for %s %ss...", len(query_list), node_type)
        res = client.querymany(query_list, scopes=scopes, fields=fields)
        logger.info("Done. %s annotation objects returned.", len(res))
        res = list2dict(res, "query")
        return res

    def annotate_curie(self, curie, raw=False, fields=None):
        """Annotate a single curie id"""
        node_type, _id = self.parse_curie(curie)
        if not node_type:
            raise InvalidCurieError(f"Unsupported Curie prefix: {curie}")
        res = self.query_biothings(node_type, [_id], fields=fields)
        if not raw:
            res = [self.transform(r) for r in res[_id]]
        return {curie: res}

    def transform(self, res):
        """perform any transformation on the annotation object, but in-place also returned object"""
        res.pop("query", None)
        res.pop("_score", None)
        # now doing field specific transformation
        transformer = ResponseTransformer()
        transformer.transform(res)
        # for path, value in traverse_keys(res):
        #     if path in ANNOTAION_FIELD_TRANSFORMATION:
        #         fn = ANNOTAION_FIELD_TRANSFORMATION[path]
        #         new_value = fn(value)
        #         set_key_value(res, path, new_value)
        return res

    def annotate_trapi(self, trapi_input, append=False, raw=False, fields=None, limit=None):
        """Annotate a TRAPI input message with node annotator annotations"""
        try:
            node_d = get_dotfield_value("message.knowledge_graph.nodes", trapi_input)
            assert isinstance(node_d, dict)
        except (KeyError, ValueError, AssertionError):
            raise TRAPIInputError("Invalid input format")

        # if limit is set, we truncate the node_d to that size
        if limit:
            _node_d = {}
            i = 0
            for node_id in node_d:
                i += 1
                if i > limit:
                    break
                _node_d[node_id] = node_d[node_id]
            node_d = _node_d
            del i, _node_d

        node_list_by_type = {}
        for node_id in node_d:
            node_type = self.parse_curie(node_id, return_type=True, return_id=False)
            if not node_type:
                logger.warning(" Unsupported Curie prefix: %s. Skipped!", node_id)
            if node_type:
                if node_type not in node_list_by_type:
                    node_list_by_type[node_type] = [node_id]
                else:
                    node_list_by_type[node_type].append(node_id)
        for node_type in node_list_by_type:
            if node_type not in self.annotator_clients or not node_list_by_type[node_type]:
                # skip for now
                continue
            # this is the list of original node ids like NCBIGene:1017, should be a unique list
            node_list = node_list_by_type[node_type]
            # this is the list of query ids like 1017
            query_list = [
                self.parse_curie(_id, return_type=False, return_id=True) for _id in node_list_by_type[node_type]
            ]
            # query_id to original id mapping
            node_id_d = dict(zip(query_list, node_list))
            res_by_id = self.query_biothings(node_type, query_list, fields=fields)
            for node_id in res_by_id:
                orig_node_id = node_id_d[node_id]
                res = res_by_id[node_id]
                if not raw:
                    if isinstance(res, list):
                        # TODO: handle multiple results here
                        res = [self.transform(r) for r in res]
                    else:
                        res = self.transform(res)
                res = {
                    "attribute_type_id": "biothings_annotations",
                    "value": res,
                }
                if append:
                    # append annotations to existing "attributes" field
                    node_d[orig_node_id]["attributes"].append(res)
                else:
                    # return annotations only
                    node_d[orig_node_id]["attributes"] = [res]

        return node_d


class AnnotatorHandler(BaseAPIHandler):
    name = "annotator"
    kwargs = {
        "*": {
            "raw": {"type": bool, "default": False},
            "fields": {"type": str, "default": None},
        },
        "POST": {
            # If True, append annotations to existing "attributes" field
            "append": {"type": bool, "default": False},
            # If set, limit the number of nodes to annotate
            "limit": {"type": int, "default": None},
        },
    }

    async def get(self, *args, **kwargs):
        annotator = Annotator()
        curie = args[0] if args else None
        if curie:
            try:
                annotated_node = annotator.annotate_curie(curie, raw=self.args.raw, fields=self.args.fields)
            except ValueError as e:
                raise HTTPError(400, reason=repr(e))
            self.finish(annotated_node)
        else:
            raise HTTPError(404, reason="missing required input curie id")

    async def post(self, *args, **kwargs):
        annotator = Annotator()
        try:
            annotated_node_d = annotator.annotate_trapi(
                self.args_json,
                append=self.args.append,
                raw=self.args.raw,
                fields=self.args.fields,
                limit=self.args.limit,
            )
        except ValueError as e:
            raise HTTPError(400, reason=repr(e))
        self.finish(annotated_node_d)