#Author-Justin Nesselrotte (resume/hang patch 2026-10-19)
#Description-Export all designs. Patched: skips finished designs BEFORE opening them, so restarts resume instantly.
#
# What changed vs. upstream (Jnesselr/fusion-360-total-exporter @ c255fc9):
#  1. Skip-before-open. A design is skipped without calling Documents.open if its folder has a
#     .export_done marker, OR (for exports made by the old script) already holds an .f3d/.f3z plus a .stp.
#     Upstream re-opened every design on every run, which is why restarts hung at the same spot.
#  2. Hang tracking. An .export_inprogress marker is written before each open and removed after.
#     If Fusion is killed while opening, the next run retries that design; after MAX_ATTEMPTS hangs
#     it is skipped and listed in skipped_hung.txt so the run can get past it.
#  3. exportignore.txt (optional, in the output folder): one design name or path fragment per line.
#  4. Per-design and per-format errors are logged and the run continues (upstream aborted).
#  5. adsk.doEvents() + short pause between designs, and documents opened as side effects
#     (assembly references) are closed after each design.
#  6. MAX_OPENS_PER_RUN: optionally stop cleanly after N opens so you can restart Fusion fresh.
#  7. The .f3d archive is no longer re-exported if it already exists.
# Output folder layout is unchanged, so an existing partial export is reused.

import adsk.core, adsk.fusion, adsk.cam, traceback

from logging import Logger, FileHandler, Formatter, INFO

import time
import os
import re
import json

# ---------------- settings ----------------
MAX_OPENS_PER_RUN = 0        # 0 = unlimited. If it still freezes, set e.g. 75, then quit/relaunch Fusion and re-run each time it stops.
MAX_ATTEMPTS = 2             # a design that hung while opening this many times gets skipped (see skipped_hung.txt)
PAUSE_BETWEEN_FILES = 0.5    # seconds; lets Fusion process cloud/UI events between designs
# ------------------------------------------

DONE_MARKER = ".export_done"
INPROGRESS_MARKER = ".export_inprogress"
FAILED_MARKER = ".export_failed"
IGNORE_FILE = "exportignore.txt"


class StopRun(Exception):
  pass


class TotalExport(object):
  def __init__(self, app):
    self.app = app
    self.ui = self.app.userInterface
    self.data = self.app.data
    self.documents = self.app.documents
    self.log = Logger("Fusion 360 Total Export")
    self.log.setLevel(INFO)
    self.num_issues = 0
    self.was_cancelled = False
    self.stopped_early = False
    self.ignore_patterns = []
    self.initial_doc_names = set()
    self.stats = {"opened": 0, "exported": 0, "skipped_done": 0, "skipped_ignored": 0,
                  "skipped_hung": 0, "skipped_failed_before": 0, "failed": 0, "not_design": 0}

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    pass

  def run(self, context):
    self.ui.messageBox(
      "Searching for and exporting files will take a while, depending on how many files you have.\n\n" \
        "You won't be able to do anything else. It has to do everything in the main thread and open and close every file.\n\n" \
          "Already-exported designs are skipped without opening them, so re-running after a freeze picks up where it left off."
      )

    output_path = self._ask_for_output_path()

    if output_path is None:
      return

    self.output_path = output_path
    file_handler = FileHandler(os.path.join(output_path, 'output.log'), 'a', 'utf-8')
    file_handler.setFormatter(Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    self.log.addHandler(file_handler)

    self._load_ignore(output_path)
    self.initial_doc_names = set(self._doc_names())

    self.log.info("Starting export! (patched script; MAX_OPENS_PER_RUN={}, MAX_ATTEMPTS={})".format(MAX_OPENS_PER_RUN, MAX_ATTEMPTS))

    self._export_data(output_path)

    summary = ", ".join("{}={}".format(k, v) for k, v in self.stats.items())
    self.log.info("Done exporting! {}".format(summary))

    if self.was_cancelled:
      self.ui.messageBox("Cancelled!\n\n" + summary)
    elif self.stopped_early:
      self.ui.messageBox("Stopped after {} opens (MAX_OPENS_PER_RUN).\n\nQuit Fusion, relaunch it, and run the script again on the same folder.\n\n{}".format(MAX_OPENS_PER_RUN, summary))
    elif self.num_issues > 0:
      self.ui.messageBox("The exporting process ran into {} issue{}. Check output.log, skipped_hung.txt and failed.txt.\n\n{}".format(
        self.num_issues, "s" if self.num_issues > 1 else "", summary))
    else:
      self.ui.messageBox("Export finished completely successfully!\n\n" + summary)

  # ---------- helpers ----------

  def _doc_names(self):
    names = []
    try:
      for i in range(self.documents.count):
        names.append(self.documents.item(i).name)
    except BaseException:
      pass
    return names

  def _close_stray_documents(self):
    # Close documents opened as a side effect (e.g. referenced components) so they don't pile up.
    try:
      for i in range(self.documents.count - 1, -1, -1):
        doc = self.documents.item(i)
        if doc.name not in self.initial_doc_names:
          try:
            doc.close(False)
            self.log.info("Closed stray document \"{}\"".format(doc.name))
          except BaseException:
            pass
    except BaseException:
      pass

  def _load_ignore(self, output_path):
    path = os.path.join(output_path, IGNORE_FILE)
    if os.path.exists(path):
      with open(path, encoding='utf-8') as f:
        for line in f:
          line = line.strip()
          if line and not line.startswith('#'):
            self.ignore_patterns.append(line.lower())
      self.log.info("Loaded {} pattern(s) from {}".format(len(self.ignore_patterns), IGNORE_FILE))

  def _is_ignored(self, file_name, rel_path):
    n = file_name.lower()
    p = rel_path.lower()
    for pat in self.ignore_patterns:
      if pat == n or pat in p:
        return True
    return False

  def _append(self, name, line):
    try:
      path = os.path.join(self.output_path, name)
      if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
          if line in (l.rstrip("\n") for l in f):
            return
      with open(path, 'a', encoding='utf-8') as f:
        f.write(line + "\n")
    except BaseException:
      pass

  def _read_attempts(self, folder):
    try:
      with open(os.path.join(folder, INPROGRESS_MARKER), encoding='utf-8') as f:
        return int(json.load(f).get("attempts", 0))
    except BaseException:
      return 1  # marker exists but unreadable: count it as one hang

  def _write_marker(self, folder, marker, payload):
    with open(os.path.join(folder, marker), 'w', encoding='utf-8') as f:
      json.dump(payload, f)

  def _remove_marker(self, folder, marker):
    try:
      os.remove(os.path.join(folder, marker))
    except FileNotFoundError:
      pass

  def _looks_exported_by_old_script(self, folder):
    # Old runs leave <name>.f3d (or .f3z) and at least one .stp in the design's folder.
    try:
      entries = os.listdir(folder)
    except BaseException:
      return False
    has_archive = any(e.lower().endswith(('.f3d', '.f3z')) and os.path.isfile(os.path.join(folder, e)) for e in entries)
    has_step = any(e.lower().endswith('.stp') for e in entries)
    return has_archive and has_step

  def _archive_exists(self, file_export_path):
    return os.path.isfile(file_export_path + ".f3d") or os.path.isfile(file_export_path + ".f3z") or os.path.isfile(file_export_path)

  # ---------- main loop ----------

  def _export_data(self, output_path):
    progress_dialog = self.ui.createProgressDialog()
    progress_dialog.show("Exporting data!", "", 0, 1, 1)

    try:
      all_hubs = self.data.dataHubs
      for hub_index in range(all_hubs.count):
        hub = all_hubs.item(hub_index)

        self.log.info("Exporting hub \"{}\"".format(hub.name))

        all_projects = hub.dataProjects
        for project_index in range(all_projects.count):
          files = []
          project = all_projects.item(project_index)
          self.log.info("Exporting project \"{}\"".format(project.name))

          folder = project.rootFolder

          files.extend(self._get_files_for(folder))

          progress_dialog.message = "Hub: {} of {}\nProject: {} of {}\nExporting design %v of %m".format(
            hub_index + 1,
            all_hubs.count,
            project_index + 1,
            all_projects.count
          )
          progress_dialog.maximumValue = max(len(files), 1)
          progress_dialog.reset()

          if not files:
            self.log.info("No files to export for this project")
            continue

          for file_index in range(len(files)):
            adsk.doEvents()
            if progress_dialog.wasCancelled:
              self.log.info("The process was cancelled!")
              self.was_cancelled = True
              return

            file = files[file_index]
            progress_dialog.progressValue = file_index + 1
            try:
              self._write_data_file(output_path, file)
            except StopRun:
              raise
            except BaseException as ex:
              # Never let one design kill the whole run.
              self.num_issues += 1
              self.stats["failed"] += 1
              self.log.exception("Unexpected error on \"{}\"".format(getattr(file, 'name', '?')), exc_info=ex)
          self.log.info("Finished exporting project \"{}\"".format(project.name))
        self.log.info("Finished exporting hub \"{}\"".format(hub.name))
    except StopRun:
      self.stopped_early = True
      self.log.info("Stopping early after {} opens (MAX_OPENS_PER_RUN)".format(self.stats["opened"]))
    finally:
      try:
        progress_dialog.hide()
      except BaseException:
        pass

  def _ask_for_output_path(self):
    folder_dialog = self.ui.createFolderDialog()
    folder_dialog.title = "Where should we store this export?"
    dialog_result = folder_dialog.showDialog()
    if dialog_result != adsk.core.DialogResults.DialogOK:
      return None

    output_path = folder_dialog.folder

    return output_path

  def _get_files_for(self, folder):
    files = []
    for file in folder.dataFiles:
      files.append(file)

    for sub_folder in folder.dataFolders:
      files.extend(self._get_files_for(sub_folder))

    return files

  def _design_folder(self, root_folder, file):
    # Same layout as upstream: <root>/Hub X/Project Y/<folders>/<name>.<ext>/
    file_folder = file.parentFolder
    file_folder_path = self._name(file_folder.name)

    while file_folder.parentFolder is not None:
      file_folder = file_folder.parentFolder
      file_folder_path = os.path.join(self._name(file_folder.name), file_folder_path)

    parent_project = file_folder.parentProject
    parent_hub = parent_project.parentHub

    rel = os.path.join(
      "Hub {}".format(self._name(parent_hub.name)),
      "Project {}".format(self._name(parent_project.name)),
      file_folder_path,
      self._name(file.name) + "." + file.fileExtension
    )
    return rel, os.path.join(root_folder, rel)

  def _write_data_file(self, root_folder, file):
    if file.fileExtension != "f3d" and file.fileExtension != "f3z":
      self.stats["not_design"] += 1
      self.log.info("Not exporting file \"{}\"".format(file.name))
      return

    # ---- decide BEFORE opening (no cloud download, no Documents.open) ----
    rel_path, file_folder_path = self._design_folder(root_folder, file)

    if self._is_ignored(file.name, rel_path):
      self.stats["skipped_ignored"] += 1
      self.log.info("Ignored (exportignore.txt): \"{}\"".format(rel_path))
      return

    if os.path.exists(os.path.join(file_folder_path, DONE_MARKER)):
      self.stats["skipped_done"] += 1
      self.log.info("Already done, skipping: \"{}\"".format(rel_path))
      return

    if os.path.exists(os.path.join(file_folder_path, FAILED_MARKER)):
      self.stats["skipped_failed_before"] += 1
      self.log.info("Failed on an earlier run, skipping (delete {} to retry): \"{}\"".format(FAILED_MARKER, rel_path))
      return

    attempts = 0
    if os.path.exists(os.path.join(file_folder_path, INPROGRESS_MARKER)):
      attempts = self._read_attempts(file_folder_path)
      if attempts >= MAX_ATTEMPTS:
        self.stats["skipped_hung"] += 1
        self.num_issues += 1
        self.log.warning("Hung {} time(s) while opening; skipping: \"{}\"".format(attempts, rel_path))
        self._append("skipped_hung.txt", rel_path)
        return
      self.log.warning("Previous run stopped while on this design ({} attempt(s)); retrying: \"{}\"".format(attempts, rel_path))
    elif self._looks_exported_by_old_script(file_folder_path):
      self._write_marker(file_folder_path, DONE_MARKER, {"source": "detected existing export", "at": time.strftime("%Y-%m-%d %H:%M:%S")})
      self.stats["skipped_done"] += 1
      self.log.info("Existing export found, marking done: \"{}\"".format(rel_path))
      return

    if MAX_OPENS_PER_RUN and self.stats["opened"] >= MAX_OPENS_PER_RUN:
      raise StopRun()

    os.makedirs(file_folder_path, exist_ok=True)
    if not os.path.exists(file_folder_path):
      self.num_issues += 1
      self.log.error("Couldn't make folder \"{}\"".format(file_folder_path))
      return

    # ---- open and export ----
    self.log.info("Exporting file \"{}\" (opening...)".format(file.name))
    self._write_marker(file_folder_path, INPROGRESS_MARKER, {"attempts": attempts + 1, "name": file.name, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    self.stats["opened"] += 1

    document = None
    errors = []
    try:
      document = self.documents.open(file)
      if document is None:
        raise Exception("Documents.open returned None")
      document.activate()
      self.log.info("Opened \"{}\"; writing to \"{}\"".format(file.name, file_folder_path))

      fusion_document = adsk.fusion.FusionDocument.cast(document)
      design = fusion_document.design
      export_manager = design.exportManager

      file_export_path = os.path.join(file_folder_path, self._name(file.name))
      if self._archive_exists(file_export_path):
        self.log.info("Archive for \"{}\" already exists".format(file.name))
      else:
        try:
          options = export_manager.createFusionArchiveExportOptions(file_export_path)
          export_manager.execute(options)
        except BaseException as ex:
          errors.append("archive")
          self.log.exception("Failed writing archive for \"{}\"".format(file.name), exc_info=ex)

      self._write_component(file_folder_path, design.rootComponent, errors)

    except BaseException as ex:
      errors.append("open/export")
      self.log.exception("Failed while working on \"{}\"".format(file.name), exc_info=ex)
    finally:
      try:
        if document is not None:
          document.close(False)
      except BaseException as ex:
        self.log.exception("Failed to close \"{}\"".format(file.name), exc_info=ex)
      self._close_stray_documents()
      self._remove_marker(file_folder_path, INPROGRESS_MARKER)

    if errors:
      self.num_issues += 1
      self.stats["failed"] += 1
      self._write_marker(file_folder_path, FAILED_MARKER, {"errors": errors, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
      self._append("failed.txt", "{}  [{}]".format(rel_path, ", ".join(errors)))
      self.log.error("Finished \"{}\" with errors: {}".format(file.name, ", ".join(errors)))
    else:
      self.stats["exported"] += 1
      self._write_marker(file_folder_path, DONE_MARKER, {"source": "exported", "at": time.strftime("%Y-%m-%d %H:%M:%S")})
      self.log.info("Finished exporting file \"{}\"".format(file.name))

    adsk.doEvents()
    if PAUSE_BETWEEN_FILES:
      time.sleep(PAUSE_BETWEEN_FILES)

  # ---------- per-component exports (same paths as upstream) ----------

  def _write_component(self, component_base_path, component, errors):
    self.log.info("Writing component \"{}\" to \"{}\"".format(component.name, component_base_path))

    output_path = os.path.join(component_base_path, self._name(component.name))

    self._guard(errors, "step " + component.name, self._write_step, output_path, component)
    self._guard(errors, "stl " + component.name, self._write_stl, output_path, component)
    self._guard(errors, "iges " + component.name, self._write_iges, output_path, component)

    sketches = component.sketches
    for sketch_index in range(sketches.count):
      sketch = sketches.item(sketch_index)
      self._guard(errors, "dxf " + sketch.name, self._write_dxf, os.path.join(output_path, sketch.name), sketch)

    occurrences = component.occurrences
    for occurrence_index in range(occurrences.count):
      occurrence = occurrences.item(occurrence_index)
      sub_component = occurrence.component
      sub_path = self._take(component_base_path, self._name(component.name))
      self._write_component(sub_path, sub_component, errors)

  def _guard(self, errors, label, fn, *args):
    try:
      fn(*args)
    except BaseException as ex:
      errors.append(label)
      self.log.exception("Failed: {}".format(label), exc_info=ex)

  def _write_step(self, output_path, component):
    file_path = output_path + ".stp"
    if os.path.exists(file_path):
      self.log.info("Step file \"{}\" already exists".format(file_path))
      return

    self.log.info("Writing step file \"{}\"".format(file_path))
    export_manager = component.parentDesign.exportManager

    options = export_manager.createSTEPExportOptions(output_path, component)
    export_manager.execute(options)

  def _write_stl(self, output_path, component):
    file_path = output_path + ".stl"
    if os.path.exists(file_path):
      self.log.info("Stl file \"{}\" already exists".format(file_path))
    else:
      self.log.info("Writing stl file \"{}\"".format(file_path))
      export_manager = component.parentDesign.exportManager

      try:
        options = export_manager.createSTLExportOptions(component, output_path)
        export_manager.execute(options)
      except BaseException as ex:
        # Empty components can't be exported as STL; only count it if there was geometry.
        if component.occurrences.count + component.bRepBodies.count + component.meshBodies.count > 0:
          raise
        self.log.info("Skipped stl for empty component \"{}\"".format(component.name))

    bRepBodies = component.bRepBodies
    meshBodies = component.meshBodies

    if (bRepBodies.count + meshBodies.count) > 0:
      self._take(output_path)
      for index in range(bRepBodies.count):
        body = bRepBodies.item(index)
        self._write_stl_body(os.path.join(output_path, body.name), body)

      for index in range(meshBodies.count):
        body = meshBodies.item(index)
        self._write_stl_body(os.path.join(output_path, body.name), body)

  def _write_stl_body(self, output_path, body):
    file_path = output_path + ".stl"
    if os.path.exists(file_path):
      self.log.info("Stl body file \"{}\" already exists".format(file_path))
      return

    self.log.info("Writing stl body file \"{}\"".format(file_path))
    export_manager = body.parentComponent.parentDesign.exportManager

    try:
      options = export_manager.createSTLExportOptions(body, file_path)
      export_manager.execute(options)
    except BaseException:
      # Probably an empty model, ignore it (upstream behaviour)
      pass

  def _write_iges(self, output_path, component):
    file_path = output_path + ".igs"
    if os.path.exists(file_path):
      self.log.info("Iges file \"{}\" already exists".format(file_path))
      return

    self.log.info("Writing iges file \"{}\"".format(file_path))

    export_manager = component.parentDesign.exportManager

    options = export_manager.createIGESExportOptions(file_path, component)
    export_manager.execute(options)

  def _write_dxf(self, output_path, sketch):
    file_path = output_path + ".dxf"
    if os.path.exists(file_path):
      self.log.info("DXF sketch file \"{}\" already exists".format(file_path))
      return

    self.log.info("Writing dxf sketch file \"{}\"".format(file_path))

    sketch.saveAsDXF(file_path)

  def _take(self, *path):
    out_path = os.path.join(*path)
    os.makedirs(out_path, exist_ok=True)
    return out_path

  def _name(self, name):
    name = re.sub('[^a-zA-Z0-9 \n\.]', '', name).strip()

    if name.endswith('.stp') or name.endswith('.stl') or name.endswith('.igs'):
      name = name[0: -4] + "_" + name[-3:]

    return name


def run(context):
  ui = None
  try:
    app = adsk.core.Application.get()

    with TotalExport(app) as total_export:
      total_export.run(context)

  except:
    ui = app.userInterface
    ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))
