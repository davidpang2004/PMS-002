#!/usr/bin/env python3
"""Generate + sign assets/DMS-Photos.shortcut (macOS only; re-run only if the
shortcut's steps change -- the signed file is committed and shipped).

The shortcut receives a DMS list.txt URL as its input, downloads that list,
then downloads every file in it and saves each to Photos.
Signing needs the Mac's `shortcuts` CLI and an internet connection.
"""
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets" / "DMS-Photos.shortcut"


def token_string(var):
    """A text field whose whole content is one variable."""
    return {"Value": {"string": "￼", "attachmentsByRange": {"{0, 1}": var}},
            "WFSerializationType": "WFTextTokenString"}


def attachment(var):
    return {"Value": var, "WFSerializationType": "WFTextTokenAttachment"}


def out(uid, name):
    return {"OutputUUID": uid, "OutputName": name, "Type": "ActionOutput"}


list_dl, split, loop_g = (str(uuid.uuid4()).upper() for _ in range(3))
file_dl, loop_end = (str(uuid.uuid4()).upper() for _ in range(2))
A = "is.workflow.actions."
actions = [
    {"WFWorkflowActionIdentifier": A + "downloadurl", "WFWorkflowActionParameters": {
        "UUID": list_dl, "WFHTTPMethod": "GET",
        "WFURL": token_string({"Type": "ExtensionInput"})}},
    {"WFWorkflowActionIdentifier": A + "text.split", "WFWorkflowActionParameters": {
        "UUID": split, "WFTextSeparator": "New Lines",
        "text": attachment(out(list_dl, "Contents of URL"))}},
    {"WFWorkflowActionIdentifier": A + "repeat.each", "WFWorkflowActionParameters": {
        "GroupingIdentifier": loop_g, "WFControlFlowMode": 0,
        "WFInput": attachment(out(split, "Split Text"))}},
    {"WFWorkflowActionIdentifier": A + "downloadurl", "WFWorkflowActionParameters": {
        "UUID": file_dl, "WFHTTPMethod": "GET",
        "WFURL": token_string({"Type": "Variable", "VariableName": "Repeat Item"})}},
    {"WFWorkflowActionIdentifier": A + "savetocameraroll", "WFWorkflowActionParameters": {
        "WFInput": attachment(out(file_dl, "Contents of URL"))}},
    {"WFWorkflowActionIdentifier": A + "repeat.each", "WFWorkflowActionParameters": {
        "GroupingIdentifier": loop_g, "WFControlFlowMode": 2, "UUID": loop_end}},
    {"WFWorkflowActionIdentifier": A + "notification", "WFWorkflowActionParameters": {
        "WFNotificationActionTitle": "DMS Photos",
        "WFNotificationActionBody": "Photos saved to your library."}},
]
shortcut = {
    "WFWorkflowClientVersion": "1200", "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientRelease": 900, "WFWorkflowHasShortcutInputVariables": True,
    "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4271458815, "WFWorkflowIconGlyphNumber": 59511},
    "WFWorkflowImportQuestions": [], "WFWorkflowTypes": [],
    "WFWorkflowInputContentItemClasses": ["WFStringContentItem", "WFURLContentItem"],
    "WFWorkflowActions": actions,
}
tmp = OUT.with_name("unsigned.shortcut")
OUT.parent.mkdir(exist_ok=True)
tmp.write_bytes(plistlib.dumps(shortcut, fmt=plistlib.FMT_BINARY))
r = subprocess.run(["shortcuts", "sign", "-m", "anyone", "-i", str(tmp), "-o", str(OUT)])
tmp.unlink(missing_ok=True)
sys.exit(r.returncode)
