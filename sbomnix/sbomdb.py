#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2022-2023 Technology Innovation Institute (TII)
#
# SPDX-License-Identifier: Apache-2.0

# pylint: disable=invalid-name, too-many-instance-attributes, too-many-arguments

""" Module for generating SBOMs in various formats """

import uuid
import logging
import json
import re
import argparse
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from reuse._licenses import LICENSE_MAP as SPDX_LICENSES
from nixgraph.graph import NixDependencies
from sbomnix.nix import Store, find_deriver
from sbomnix.utils import (
    LOGGER_NAME,
    df_to_csv_file,
    get_py_pkg_version,
)

###############################################################################

_LOG = logging.getLogger(LOGGER_NAME)

###############################################################################


class SbomDb:
    """Generates SBOMs in various formats"""

    def __init__(
        self, nix_path, runtime=True, buildtime=False, meta_path=None, depth=None
    ):
        # self.uid specifies the attribute that SbomDb uses as unique
        # identifier for the sbom components. See the column names in
        # self.df_sbomdb (sbom.csv) for a list of all components' attributes.
        self.uid = "store_path"
        self.runtime = runtime
        self.buildtime = buildtime
        self.meta_path = meta_path
        self.target_deriver = find_deriver(nix_path)
        self.df_rdeps = None
        self.df_bdeps = None
        self.depth = depth
        self._init_dependencies(nix_path)
        self.df_sbomdb = None
        self.df_sbomdb_out_exploded = None
        self._init_sbomdb()
        self.uuid = uuid.uuid4()
        self.sbom_type = "runtime_and_buildtime"
        if self.runtime and not self.buildtime:
            self.sbom_type = "runtime_only"
        elif not self.runtime and self.buildtime:
            self.sbom_type = "buildtime_only"

    def _init_dependencies(self, nix_path):
        """Initialize runtime and buildtime dependencies (df_rdeps, df_bdeps)"""
        if self.runtime:
            runtime_dependencies = NixDependencies(nix_path, buildtime=False)
            self.df_rdeps = self._get_dependencies_df(runtime_dependencies)
        if self.buildtime:
            buildtime_dependencies = NixDependencies(nix_path, buildtime=True)
            self.df_bdeps = self._get_dependencies_df(buildtime_dependencies)

    def _get_dependencies_df(self, nix_dependencies):
        if self.depth:
            # Return dependencies until the given depth
            _LOG.debug("Reading dependencies until depth=%s", self.depth)
            args = argparse.Namespace()
            args.depth = self.depth
            args.return_df = True
            return nix_dependencies.graph(args)
        # Otherwise, return all dependencies
        _LOG.debug("Reading all dependencies")
        return nix_dependencies.to_dataframe()

    def _init_sbomdb(self):
        """Initialize self.df_sbomdb"""
        if (self.df_bdeps is None or self.df_bdeps.empty) and (
            self.df_rdeps is None or self.df_rdeps.empty
        ):
            # No dependencies, so the only component in the sbom
            # will be the target itself
            paths = set([self.target_deriver])
        else:
            # Concat buildtime and runtime dependencies dropping duplicates
            df_paths = pd.concat([self.df_rdeps, self.df_bdeps], ignore_index=True)
            # Get unique src_paths and target_paths
            src_paths = df_paths["src_path"].unique().tolist()
            target_paths = df_paths["target_path"].unique().tolist()
            paths = set(src_paths + target_paths)
        # Populate store based on the dependencies
        store = Store(self.buildtime)
        for path in paths:
            store.add_path(path)
        self.df_sbomdb = store.to_dataframe()
        # Join with meta information
        self._sbomdb_join_meta(self.meta_path)
        # Clean, drop duplicates, sort
        self.df_sbomdb.replace(np.nan, "", regex=True, inplace=True)
        self.df_sbomdb.drop_duplicates(subset=[self.uid], keep="first", inplace=True)
        self.df_sbomdb.sort_values(by=["name", self.uid], inplace=True)
        self.df_sbomdb_out_exploded = self.df_sbomdb.explode("out")

    def _sbomdb_join_meta(self, meta_path):
        """Join self.df_sbomdb with meta information"""
        if meta_path is None:
            return
        df_meta = _parse_json_metadata(meta_path)
        if _LOG.level <= logging.DEBUG:
            df_to_csv_file(df_meta, "meta.csv")
        # Join based on package name including the version number
        self.df_sbomdb = self.df_sbomdb.merge(
            df_meta,
            how="left",
            left_on=["name"],
            right_on=["name"],
            suffixes=["", "_meta"],
        )

    def _lookup_dependencies(self, drv, uid="store_path"):
        """
        Lookup the runtime and buildtime dependencies for `drv`.
        Returns a list of unique dependencies as specified by the
        dependencies' attribute (column) `uid` values.
        By default, returns the list of unique `store_path` values
        which includes the build and runtime dependencies of `drv`.
        """
        # Find runtime dependencies
        # Runtime dependencies: drv.out matches with target_path
        dfr = None
        if self.df_rdeps is not None and not self.df_rdeps.empty:
            df = self.df_rdeps[self.df_rdeps["target_path"].isin(drv.out)]
            # Find the requested 'uid' values for the dependencies (df.src_path)
            dfr = self.df_sbomdb_out_exploded.merge(
                df, how="inner", left_on=["out"], right_on=["src_path"]
            ).loc[:, [uid]]
        # Find buildtime dependencies
        dfb = None
        if self.df_bdeps is not None and not self.df_bdeps.empty:
            # Buildtime dependencies: drv.store_path matches with target_path
            df = self.df_bdeps[self.df_bdeps["target_path"] == drv.store_path]
            # Find the requested 'uid' values for the dependencies (df.src_path)
            dfb = self.df_sbomdb.merge(
                df, how="inner", left_on=["store_path"], right_on=["src_path"]
            ).loc[:, [uid]]
        # Return list of unique dependencies including both build and runtime
        # dependencies as specified by the requested values (uid)
        if dfr is not None or dfb is not None:
            df = pd.concat([dfr, dfb], ignore_index=True)
            dep_uids = df[uid].unique().tolist()
            # Filter out dependencies to drv itself
            self_uid = getattr(drv, uid)
            return [uid for uid in dep_uids if uid != self_uid]
        return None

    def _write_json(self, pathname, data, printinfo=False):
        with open(pathname, "w", encoding="utf-8") as outfile:
            json_string = json.dumps(data, indent=2)
            outfile.write(json_string)
            if printinfo:
                _LOG.info("Wrote: %s", outfile.name)

    def to_cdx(self, cdx_path, printinfo=True):
        """Export sbomdb to cyclonedx json file"""
        cdx = {}
        cdx["bomFormat"] = "CycloneDX"
        cdx["specVersion"] = "1.3"
        cdx["version"] = 1
        cdx["serialNumber"] = f"urn:uuid:{self.uuid}"
        cdx["metadata"] = {}
        cdx["metadata"]["timestamp"] = (
            datetime.now(timezone.utc).astimezone().isoformat()
        )
        cdx["metadata"]["properties"] = []
        prop = {}
        prop["name"] = "sbom_type"
        prop["value"] = self.sbom_type
        cdx["metadata"]["properties"].append(prop)
        if self.depth:
            prop = {}
            prop["name"] = "sbom_dependencies_depth"
            prop["value"] = self.depth
            cdx["metadata"]["properties"].append(prop)
        tool = {}
        tool["vendor"] = "TII"
        tool["name"] = "sbomnix"
        tool["version"] = get_py_pkg_version()
        cdx["metadata"]["tools"] = []
        cdx["metadata"]["tools"].append(tool)
        cdx["components"] = []
        cdx["dependencies"] = []
        for drv in self.df_sbomdb.itertuples():
            component = _drv_to_cdx_component(drv, uid=self.uid)
            if drv.store_path == self.target_deriver:
                cdx["metadata"]["component"] = component
            else:
                cdx["components"].append(component)
            deps = self._lookup_dependencies(drv, uid=self.uid)
            dependency = _drv_to_cdx_dependency(drv, deps, uid=self.uid)
            cdx["dependencies"].append(dependency)
        self._write_json(cdx_path, cdx, printinfo)

    def to_spdx(self, spdx_path, printinfo=True):
        """Export sbomdb to spdx json file"""
        spdx = {}
        spdx["spdxVersion"] = "SPDX-2.3"
        spdx["dataLicense"] = "CC0-1.0"
        spdx["SPDXID"] = "SPDXRef-DOCUMENT"
        spdx["name"] = ""
        spdx["documentNamespace"] = f"sbomnix://{self.uuid}"
        creation_info = {}
        creation_info["created"] = datetime.now(timezone.utc).astimezone().isoformat()
        creation_info["creators"] = []
        creation_info["creators"].append(f"Tool: sbomnix-{get_py_pkg_version()}")
        spdx["creationInfo"] = creation_info
        spdx["comment"] = f"included dependencies: '{self.sbom_type}'"
        spdx["packages"] = []
        spdx["relationships"] = []
        for drv in self.df_sbomdb.itertuples():
            package = _drv_to_spdx_package(drv, uid=self.uid)
            spdx["packages"].append(package)
            if drv.store_path == self.target_deriver:
                spdx["name"] = _str_to_spdxid(getattr(drv, self.uid))
            deps = self._lookup_dependencies(drv, uid=self.uid)
            relationships = _drv_to_spdx_relationships(drv, deps, uid=self.uid)
            for relation in relationships:
                spdx["relationships"].append(relation)
        self._write_json(spdx_path, spdx, printinfo)

    def to_csv(self, csv_path, loglevel=logging.INFO):
        """Export sbomdb to csv file"""
        df_to_csv_file(self.df_sbomdb, csv_path, loglevel)


################################################################################

# SPDX


def _str_to_spdxid(strval):
    # Only letters, numbers, '.', and '-' are allowed in spdx idstring,
    # replace all other characters with '-'
    idstring = re.sub(r"[^\-.a-zA-Z0-9]", "-", strval)
    # Return idstring with prefix "SPDXRef-"
    if idstring.startswith("-"):
        return f"SPDXRef{idstring}"
    return f"SPDXRef-{idstring}"


def _drv_to_spdx_license_list(drv):
    license_attr_name = "meta_license_spdxid"
    if license_attr_name not in drv._asdict():
        return []
    license_str = getattr(drv, license_attr_name)
    if not license_str:
        return []
    license_strings = license_str.split(";")
    licenses = []
    for license_string in license_strings:
        if license_string not in SPDX_LICENSES:
            continue
        licenses.append(license_string)
    return licenses


def _drv_to_spdx_extrefs(drv):
    extrefs = []
    if drv.cpe:
        cpe_ref = {}
        cpe_ref["referenceCategory"] = "SECURITY"
        cpe_ref["referenceType"] = "cpe23Type"
        cpe_ref["referenceLocator"] = drv.cpe
        extrefs.append(cpe_ref)
    if drv.purl:
        purl_ref = {}
        purl_ref["referenceCategory"] = "PACKAGE-MANAGER"
        purl_ref["referenceType"] = "purl"
        purl_ref["referenceLocator"] = drv.purl
        extrefs.append(purl_ref)
    return extrefs


def _drv_to_spdx_package(drv, uid="store_path"):
    """Convert one entry from sbomdb (drv) to spdx package"""
    pkg = {}
    pkg["name"] = drv.pname
    pkg["SPDXID"] = _str_to_spdxid(getattr(drv, uid))
    pkg["versionInfo"] = drv.version
    pkg["downloadLocation"] = "NOASSERTION"
    if drv.urls:
        pkg["downloadLocation"] = drv.urls
    if "meta_homepage" in drv._asdict() and drv.meta_homepage:
        pkg["homepage"] = drv.meta_homepage
    if "meta_description" in drv._asdict() and drv.meta_description:
        pkg["summary"] = drv.meta_description
    licenses = _drv_to_spdx_license_list(drv)
    if licenses:
        pkg["licenseInfoFromFiles"] = licenses
    licence_entry = licenses[0] if len(licenses) == 1 else "NOASSERTION"
    pkg["licenseConcluded"] = licence_entry
    pkg["licenseDeclared"] = licence_entry
    pkg["copyrightText"] = "NOASSERTION"
    extrefs = _drv_to_spdx_extrefs(drv)
    if extrefs:
        pkg["externalRefs"] = extrefs
    return pkg


def _drv_to_spdx_relationships(drv, deps_list, uid="store_path"):
    """Return list of spdx relationship structures for sbomdb drv"""
    relationships = []
    if not deps_list:
        return relationships
    drv_spdxid = _str_to_spdxid(getattr(drv, uid))
    relationship_type = "DEPENDS_ON"
    for dep in deps_list:
        relationship = {}
        relationship["spdxElementId"] = drv_spdxid
        relationship["relationshipType"] = relationship_type
        relationship["relatedSpdxElement"] = _str_to_spdxid(dep)
        relationships.append(relationship)
    return relationships


################################################################################

# CycloneDX


def _drv_to_cdx_licenses_entry(drv, column_name, cdx_license_type):
    """Parse license entries of type cdx_license_type from column_name"""
    licenses = []
    if column_name not in drv._asdict():
        # Return empty list if column name is not in drv
        return licenses
    license_str = getattr(drv, column_name)
    if not license_str:
        # Return empty list if license string is empty
        return licenses
    # Parse the ";" separated licenses to cdx license format
    license_strings = license_str.split(";")
    for license_string in license_strings:
        # Give up generating the 'licenses' entry if license id should be
        # spdx but it's not:
        if "spdxid" in column_name and license_string not in SPDX_LICENSES:
            _LOG.debug("Invalid spdxid license '%s':'%s'", drv.name, license_string)
            return []
        license_dict = {"license": {cdx_license_type: license_string}}
        licenses.append(license_dict)
    return licenses


def _cdx_component_add_licenses(component, drv):
    """Add licenses array to cdx component (if any)"""
    licenses = []
    # First, try reading the license in spdxid-format
    licenses = _drv_to_cdx_licenses_entry(drv, "meta_license_spdxid", "id")
    # If it fails, try reading the license short name
    if not licenses:
        licenses = _drv_to_cdx_licenses_entry(drv, "meta_license_short", "name")
    # Give up if package does not have license information associated
    if not licenses:
        _LOG.debug("No license info found for '%s'", drv.name)
        return
    # Otherwise, add the licenses entry
    component["licenses"] = licenses


def _drv_to_cdx_component(drv, uid="store_path"):
    """Convert one entry from sbomdb (drv) to cdx component"""
    component = {}
    component["type"] = "application"
    component["bom-ref"] = getattr(drv, uid)
    component["name"] = drv.pname
    component["version"] = drv.version
    if drv.purl:
        component["purl"] = drv.purl
    if drv.cpe:
        component["cpe"] = drv.cpe
    if "meta_description" in drv._asdict() and drv.meta_description:
        component["description"] = drv.meta_description
    _cdx_component_add_licenses(component, drv)
    properties = []
    for out_path in drv.out:
        prop = {}
        prop["name"] = "nix:out_path"
        prop["value"] = out_path
        properties.append(prop)
    if drv.store_path:
        prop = {}
        prop["name"] = "nix:drv_path"
        prop["value"] = drv.store_path
        properties.append(prop)
    # To externalReferences?
    if drv.urls:
        prop = {}
        prop["name"] = "nix:fetch_url"
        prop["value"] = drv.urls
        properties.append(prop)
    if "meta_homepage" in drv._asdict() and drv.meta_homepage:
        prop = {}
        prop["name"] = "homepage"
        prop["value"] = drv.meta_homepage
        properties.append(prop)
    if properties:
        component["properties"] = properties
    return component


def _drv_to_cdx_dependency(drv, deps_list, uid="store_path"):
    """Return cdx dependency structure for sbomdb drv"""
    dependency = {}
    dependency["ref"] = getattr(drv, uid)
    if deps_list:
        dependency["dependsOn"] = deps_list
    return dependency


###############################################################################

# Nix package metadata


def _parse_meta_entry(meta, key):
    """Parse the given key from the metadata entry"""
    if isinstance(meta, dict):
        ret = [meta.get(key, "")]
    elif isinstance(meta, list):
        ret = [x.get(key, "") if isinstance(x, dict) else x for x in meta]
    else:
        ret = [meta]
    return list(filter(None, ret))


def _parse_json_metadata(json_filename):
    """Parse package metadata from the specified json file"""
    with open(json_filename, "r", encoding="utf-8") as inf:
        _LOG.info('Loading meta info from "%s"', json_filename)
        json_dict = json.loads(inf.read())
        dict_selected = {}
        setcol = dict_selected.setdefault
        for nixpkg_name, pkg in json_dict.items():
            # generic package info
            setcol("nixpkgs", []).append(nixpkg_name)
            setcol("name", []).append(pkg.get("name", ""))
            setcol("pname", []).append(pkg.get("pname", ""))
            setcol("version", []).append(pkg.get("version", ""))
            # meta
            meta = pkg.get("meta", {})
            setcol("meta_homepage", []).append(meta.get("homepage", ""))
            setcol("meta_position", []).append(meta.get("position", ""))
            setcol("meta_unfree", []).append(meta.get("unfree", ""))
            setcol("meta_description", []).append(meta.get("description", ""))
            # meta.license
            meta_license = meta.get("license", {})
            license_short = _parse_meta_entry(meta_license, key="shortName")
            setcol("meta_license_short", []).append(";".join(license_short))
            license_spdx = _parse_meta_entry(meta_license, key="spdxId")
            setcol("meta_license_spdxid", []).append(";".join(license_spdx))
            # meta.maintainers
            meta_maintainers = meta.get("maintainers", {})
            emails = _parse_meta_entry(meta_maintainers, key="email")
            setcol("meta_maintainers_email", []).append(";".join(emails))
        return pd.DataFrame(dict_selected)


################################################################################
