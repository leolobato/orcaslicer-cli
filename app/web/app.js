/* ============================================================
   OrcaSlicer Profile Manager — Alpine.js Application
   ============================================================
   All components live in this single file for simplicity.
   Hash-based routing drives which view is displayed.
   ============================================================ */

// ---------------------------------------------------------------------------
// API helper — all fetch calls go through here for consistent error handling
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const msg = (body && body.error) || resp.statusText || "Request failed";
    throw new Error(msg);
  }
  return body;
}

// ---------------------------------------------------------------------------
// Hash router — parses window.location.hash into a route object
// ---------------------------------------------------------------------------

function parseHash(hash) {
  // Remove leading "#" and split off query string
  const raw = (hash || "").replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  const segments = pathPart.split("/").filter(Boolean).map(decodeURIComponent);

  // Parse query params (only "filter" used currently)
  const params = {};
  if (queryPart) {
    queryPart.split("&").forEach((pair) => {
      const [k, v] = pair.split("=");
      if (k) params[k] = decodeURIComponent(v || "");
    });
  }

  // Default / empty -> redirect to filaments
  if (segments.length === 0) {
    return { type: "filaments", view: "list", id: null, filter: null };
  }

  const type = segments[0]; // machines | processes | filaments

  // #/filaments/new
  if (segments.length === 2 && segments[1] === "new") {
    return { type, view: "new", id: null, filter: null };
  }

  // #/filaments/GFL99/edit
  if (segments.length === 3 && segments[2] === "edit") {
    return { type, view: "edit", id: segments[1], filter: null };
  }

  // #/machines/GM014  (inspect detail)
  if (segments.length === 2) {
    return { type, view: "inspect", id: segments[1], filter: null };
  }

  // #/filaments?filter=user  (list with optional filter)
  return { type, view: "list", id: null, filter: params.filter || null };
}

// ---------------------------------------------------------------------------
// Formatting helper for displaying profile values
// ---------------------------------------------------------------------------

function formatValue(val) {
  if (val === null || val === undefined) return "-";
  if (typeof val === "object") return JSON.stringify(val);
  return String(val);
}

/**
 * Group filament fields into categories for the editor form.
 * Returns an array of { name, fields, hasModified } objects.
 *
 * Rules are checked in order; the first match wins. Ordering matters:
 *   - Build Plate runs before Cooling & Fan so `cool_plate_temp` isn't
 *     swept into cooling by the word "cool".
 *   - Multi-material runs before Cooling & Fan so `filament_cooling_*`
 *     (wipe-tower ramming moves) stays with the other tool-change keys.
 */
function categorizeFields(fields) {
  const categories = [
    {
      name: "Material",
      test: (k) =>
        /^filament_(type|vendor|cost|density|colour|color|diameter|weight|soluble|is_support|shrink|id$|extruder_variant)/.test(k) ||
        k === "required_nozzle_HRC" ||
        k === "default_filament_colour",
    },
    {
      name: "Temperature",
      test: (k) =>
        /^(nozzle_temperature|chamber_temperature|idle_temperature|temperature_vitrification|filament_vitrification_temperature)/.test(k) ||
        k === "activate_chamber_temp_control",
    },
    {
      name: "Build Plate",
      test: (k) => /_plate_temp(_initial_layer)?$/.test(k),
    },
    {
      name: "Seam (Scarf)",
      test: (k) => /^filament_scarf_/.test(k),
    },
    {
      name: "Ironing",
      test: (k) => /^filament_ironing_/.test(k),
    },
    {
      name: "Multi-material",
      test: (k) =>
        /^filament_(loading|unloading|ramming|cooling|multitool|toolchange|stamping|minimal_purge|map)/.test(k),
    },
    {
      name: "Retraction & Z-hop",
      test: (k) => /retract|deretract|wipe|z_hop/.test(k),
    },
    {
      name: "Flow & Pressure",
      test: (k) =>
        /flow_ratio|pressure_advance|max_volumetric_speed/.test(k) ||
        k === "enable_pressure_advance",
    },
    {
      name: "Cooling & Fan",
      test: (k) =>
        /fan|cooling|slow_down|close_fan|full_fan_speed_layer|reduce_fan_stop_start_freq|dont_slow_down_outer_wall/.test(k) ||
        k === "activate_air_filtration",
    },
    {
      name: "Custom G-code",
      test: (k) => /^filament_(start_gcode|end_gcode|notes)$/.test(k),
    },
  ];

  const groups = categories.map((c) => ({ name: c.name, fields: [], hasModified: false }));
  const other = { name: "Other", fields: [], hasModified: false };

  for (const field of fields) {
    let placed = false;
    for (let i = 0; i < categories.length; i++) {
      if (categories[i].test(field.key)) {
        groups[i].fields.push(field);
        if (field.modified) groups[i].hasModified = true;
        placed = true;
        break;
      }
    }
    if (!placed) {
      other.fields.push(field);
      if (field.modified) other.hasModified = true;
    }
  }

  // Sort fields within each group alphabetically
  for (const g of groups) g.fields.sort((a, b) => a.key.localeCompare(b.key));
  other.fields.sort((a, b) => a.key.localeCompare(b.key));

  // Return non-empty groups, with Other at the end
  return [...groups, other].filter((g) => g.fields.length > 0);
}

/** Unwrap single-element arrays for cleaner editing (["220"] -> "220"). */
function formatEditValue(val) {
  if (Array.isArray(val) && val.length === 1) return String(val[0]);
  return formatValue(val);
}

/** Check if a value is a single-element array (OrcaSlicer's common pattern). */
function isSingleArray(val) {
  return Array.isArray(val) && val.length === 1;
}

/**
 * Detect whether a parsed profile JSON describes a filament, process, or
 * machine. Mirrors the server-side `_detect_profile_type` enough to catch
 * cross-category import attempts before they hit the API and produce a
 * misleading "parent not found" error. Returns null when it can't tell.
 */
function detectProfileCategory(parsed) {
  if (!parsed || typeof parsed !== "object") return null;
  const t = typeof parsed.type === "string" ? parsed.type.toLowerCase() : "";
  if (t === "filament" || t === "process" || t === "machine") return t;
  if ("filament_type" in parsed || "filament_id" in parsed
      || "filament_settings_id" in parsed) {
    return "filament";
  }
  if ("nozzle_diameter" in parsed || "printer_model" in parsed
      || "printer_variant" in parsed) {
    return "machine";
  }
  if ("print_settings_id" in parsed || "layer_height" in parsed
      || "outer_wall_speed" in parsed || "sparse_infill_speed" in parsed) {
    return "process";
  }
  return null;
}

// ---------------------------------------------------------------------------
// COMPONENT: app() — Main app shell, routing, and shared state
// ---------------------------------------------------------------------------

function app() {
  return {
    // Shared state
    version: "",
    route: parseHash(window.location.hash),
    detail: null,    // loaded profile detail for inspector
    loading: false,
    reloading: false,

    // Global machine filter — filters processes and filaments
    machines: [],
    selectedMachineId: "",  // setting_id or "" for "All"

    get selectedMachineName() {
      if (!this.selectedMachineId) return "";
      const m = this.machines.find((x) => x.setting_id === this.selectedMachineId);
      return m ? m.name : this.selectedMachineId;
    },

    async loadMachines() {
      try {
        this.machines = await api("/profiles/machines");
        // Restore persisted filter after options are available
        const stored = localStorage.getItem("machineFilter") || "";
        if (stored && this.machines.some((m) => m.setting_id === stored)) {
          this.selectedMachineId = stored;
        } else if (stored) {
          // Machine no longer exists — clear
          this.selectedMachineId = "";
          localStorage.removeItem("machineFilter");
        }
      } catch (err) {
        console.error("Failed to load machines for filter:", err);
      }
    },

    selectMachine(settingId) {
      this.selectedMachineId = settingId;
      if (settingId) {
        localStorage.setItem("machineFilter", settingId);
      } else {
        localStorage.removeItem("machineFilter");
      }
      // Re-trigger current list view to reload with filter
      window.dispatchEvent(new Event("machine-filter-changed"));
    },

    init() {
      // Restore persisted machine filter
      this.selectedMachineId = localStorage.getItem("machineFilter") || "";

      // Load API version and machine list for global filter
      api("/health").then((data) => {
        this.version = data.version || "";
      }).catch(() => {});
      this.loadMachines();

      // Listen for hash changes
      window.addEventListener("hashchange", () => {
        this.route = parseHash(window.location.hash);
        this.detail = null;

        // Load detail when navigating to an inspect view
        if (this.route.view === "inspect" && this.route.id) {
          this.loadDetail();
        }
      });

      // If we opened directly to an inspect route, load it now
      if (this.route.view === "inspect" && this.route.id) {
        this.loadDetail();
      }

      // Default redirect
      if (!window.location.hash || window.location.hash === "#" || window.location.hash === "#/") {
        window.location.hash = "#/filaments";
      }
    },

    // Load a single profile detail (for inspector)
    async loadDetail() {
      if (!this.route.type || !this.route.id) return;
      this.loading = true;
      this.detail = null;
      try {
        this.detail = await api(`/profiles/${this.route.type}/${encodeURIComponent(this.route.id)}`);
      } catch (err) {
        alert("Failed to load profile: " + err.message);
      } finally {
        this.loading = false;
      }
    },

    // Reload all profiles from disk
    async reloadProfiles() {
      this.reloading = true;
      try {
        await api("/profiles/reload", { method: "POST" });
        // Re-trigger the current view so lists refresh
        window.dispatchEvent(new Event("hashchange"));
      } catch (err) {
        alert("Reload failed: " + err.message);
      } finally {
        this.reloading = false;
      }
    },
  };
}

// ---------------------------------------------------------------------------
// COMPONENT: machineList() — Loads and displays machine profiles
// ---------------------------------------------------------------------------

function machineList() {
  return {
    items: [],
    search: "",
    listLoading: false,

    get filteredItems() {
      const q = this.search.toLowerCase().trim();
      if (!q) return this.items;
      return this.items.filter(
        (m) =>
          (m.name || "").toLowerCase().includes(q) ||
          (m.setting_id || "").toLowerCase().includes(q)
      );
    },

    async load() {
      this.listLoading = true;
      try {
        this.items = await api("/profiles/machines");
      } catch (err) {
        alert("Failed to load machines: " + err.message);
      } finally {
        this.listLoading = false;
      }
    },

    // Navigate to the inspector for this profile
    inspect(type, id) {
      window.location.hash = `#/${type}/${encodeURIComponent(id)}`;
    },
  };
}

// ---------------------------------------------------------------------------
// COMPONENT: processList() — Loads and displays process profiles
// ---------------------------------------------------------------------------

function processList() {
  return {
    items: [],
    search: "",
    listLoading: false,
    importer: importProfileModal("processes", () => {
      window.location.hash = "#/processes?filter=user";
      setTimeout(() => window.dispatchEvent(new Event("machine-filter-changed")), 50);
    }),

    get filteredItems() {
      const q = this.search.toLowerCase().trim();
      let list = this.items;

      const route = this.$data.route;
      if (route && route.filter === "user") {
        list = list.filter((p) => p.vendor === "User");
      }

      if (!q) return list;
      return list.filter(
        (p) =>
          (p.name || "").toLowerCase().includes(q) ||
          (p.setting_id || "").toLowerCase().includes(q)
      );
    },

    async load() {
      this.listLoading = true;
      try {
        const machineId = this.$data.selectedMachineId;
        const qs = machineId ? `?machine=${encodeURIComponent(machineId)}` : "";
        this.items = await api(`/profiles/processes${qs}`);
      } catch (err) {
        alert("Failed to load processes: " + err.message);
      } finally {
        this.listLoading = false;
      }
      // Reload when machine filter changes
      window.addEventListener("machine-filter-changed", () => this.load(), { once: true });
    },

    inspect(type, id) {
      window.location.hash = `#/${type}/${encodeURIComponent(id)}`;
    },

    async deleteProcess(p) {
      if (!confirm(`Delete user process profile "${p.name}" (${p.setting_id})?`)) return;
      try {
        await api(`/profiles/processes/${encodeURIComponent(p.setting_id)}`, { method: "DELETE" });
        this.items = this.items.filter((x) => x.setting_id !== p.setting_id);
      } catch (err) {
        alert("Delete failed: " + err.message);
      }
    },
  };
}

// ---------------------------------------------------------------------------
// COMPONENT: filamentList() — Loads and displays filament profiles
// ---------------------------------------------------------------------------

function filamentList() {
  return {
    items: [],
    search: "",
    listLoading: false,
    importer: importProfileModal("filaments", () => {
      window.location.hash = "#/filaments?filter=user";
      setTimeout(() => window.dispatchEvent(new Event("machine-filter-changed")), 50);
    }),

    get filteredItems() {
      const q = this.search.toLowerCase().trim();
      let list = this.items;

      // Apply user filter from route
      const route = this.$data.route;
      if (route && route.filter === "user") {
        list = list.filter((f) => f.vendor === "User");
      }

      if (!q) return list;
      return list.filter(
        (f) =>
          (f.name || "").toLowerCase().includes(q) ||
          (f.setting_id || "").toLowerCase().includes(q) ||
          (f.filament_id || "").toLowerCase().includes(q) ||
          (f.filament_type || "").toLowerCase().includes(q)
      );
    },

    async load() {
      this.listLoading = true;
      try {
        const machineId = this.$data.selectedMachineId;
        const qs = machineId ? `?machine=${encodeURIComponent(machineId)}` : "";
        this.items = await api(`/profiles/filaments${qs}`);
      } catch (err) {
        alert("Failed to load filaments: " + err.message);
      } finally {
        this.listLoading = false;
      }
      // Reload when machine filter changes
      window.addEventListener("machine-filter-changed", () => this.load(), { once: true });
    },

    inspect(type, id) {
      window.location.hash = `#/${type}/${encodeURIComponent(id)}`;
    },

    editFilament(id) {
      window.location.hash = `#/filaments/${encodeURIComponent(id)}/edit`;
    },

    async deleteFilament(filament) {
      if (!confirm(`Delete "${filament.name}" (${filament.setting_id})?`)) return;
      try {
        await api(`/profiles/filaments/${encodeURIComponent(filament.setting_id)}`, { method: "DELETE" });
        // Remove from local list
        this.items = this.items.filter((f) => f.setting_id !== filament.setting_id);
      } catch (err) {
        alert("Delete failed: " + err.message);
      }
    },
  };
}

// ---------------------------------------------------------------------------
// COMPONENT: inspector() — Tabs for resolved view and inheritance diff
// ---------------------------------------------------------------------------

function inspector() {
  return {
    tab: "resolved",

    initInspector() {
      // Reset to resolved tab when opening
      this.tab = "resolved";
    },

    // The detail object comes from the parent app() component via $data
    // (Alpine.js merges data scopes, so this.detail refers to app().detail)

    // Sorted key-value pairs from the resolved profile
    resolvedEntries() {
      const detail = this.$data.detail;
      if (!detail || !detail.resolved) return [];
      return Object.keys(detail.resolved)
        .sort()
        .map((key) => ({
          key,
          display: formatValue(detail.resolved[key]),
        }));
    },

    // Sorted own_fields entries for a given chain level, with class info
    ownFieldEntries(level, levelIndex) {
      const fields = level.own_fields || {};
      return Object.keys(fields)
        .sort()
        .map((key) => {
          const val = fields[key];
          const cls = this.fieldClass(key, levelIndex);
          const parentDisplay = this.parentValue(key, levelIndex);
          return {
            key,
            display: formatValue(val),
            parentDisplay,
            cls,
          };
        });
    },

    // Determine if a field is "introduced" (new at this level) or
    // "overridden" (exists in a parent with a different value)
    fieldClass(key, levelIndex) {
      const detail = this.$data.detail;
      if (!detail || !detail.inheritance_chain) return "";
      const chain = detail.inheritance_chain;

      // Search parent levels (levels after this one) for the same key
      for (let i = levelIndex + 1; i < chain.length; i++) {
        const parentFields = chain[i].own_fields || {};
        if (key in parentFields) {
          return "bg-amber-900/20 border-l-2 border-amber-600";
        }
      }
      // Not found in any parent -> introduced at this level
      return "bg-emerald-900/20 border-l-2 border-emerald-600";
    },

    // Find the parent value for a field (first occurrence in later levels)
    parentValue(key, levelIndex) {
      const detail = this.$data.detail;
      if (!detail || !detail.inheritance_chain) return "-";
      const chain = detail.inheritance_chain;

      for (let i = levelIndex + 1; i < chain.length; i++) {
        const parentFields = chain[i].own_fields || {};
        if (key in parentFields) {
          return formatValue(parentFields[key]);
        }
      }
      return "-";
    },

    // Navigate to the edit form for the current user filament
    editFilament() {
      const detail = this.$data.detail;
      if (!detail || !detail.setting_id) return;
      window.location.hash = `#/filaments/${encodeURIComponent(detail.setting_id)}/edit`;
    },

    // Delete the current user filament and return to the filament list
    async deleteFilament() {
      const detail = this.$data.detail;
      if (!detail || !detail.setting_id) return;
      if (!confirm(`Delete "${detail.name}" (${detail.setting_id})?`)) return;
      try {
        await api(`/profiles/filaments/${encodeURIComponent(detail.setting_id)}`, { method: "DELETE" });
        window.location.hash = "#/filaments?filter=user";
      } catch (err) {
        alert("Delete failed: " + err.message);
      }
    },

    // Copy the full resolved JSON to clipboard
    copyJson() {
      const detail = this.$data.detail;
      if (!detail || !detail.resolved) return;
      const text = JSON.stringify(detail.resolved, null, 2);
      navigator.clipboard.writeText(text).then(
        () => {},
        () => alert("Failed to copy to clipboard")
      );
    },
  };
}

// ---------------------------------------------------------------------------
// COMPONENT: filamentEditor() — Create or edit a custom filament profile
// ---------------------------------------------------------------------------

function filamentEditor() {
  return {
    step: 1,

    // Step 1 state
    allFilaments: [],
    parentSearch: "",
    selectedParent: null,
    selectedParentData: null,
    parentDetail: null, // full resolved parent for field editing

    // Step 2 state
    profileName: "",
    editableFields: [],
    fieldGroups: [],

    // Step 3 state
    preview: null,
    previewLoading: false,
    previewError: null,
    saving: false,
    saveError: null,

    // For edit mode
    editingId: null,

    async initEditor() {
      this.step = 1;
      this.preview = null;
      this.previewError = null;
      this.saveError = null;

      // Load all filaments for parent picker
      try {
        this.allFilaments = await api("/profiles/filaments");
      } catch (err) {
        alert("Failed to load filaments: " + err.message);
      }

      // If editing, load the existing profile and pre-populate
      const route = this.$data.route;
      if (route.view === "edit" && route.id) {
        this.editingId = route.id;
        try {
          const detail = await api(`/profiles/filaments/${encodeURIComponent(route.id)}`);
          this.profileName = detail.name || "";

          // Find the parent from the inheritance chain (second entry, index 1)
          if (detail.inheritance_chain && detail.inheritance_chain.length > 1) {
            const parentLevel = detail.inheritance_chain[1];
            const parentMatch = this.allFilaments.find(
              (f) => f.name === parentLevel.name
            );
            if (parentMatch) {
              this.selectedParent = parentMatch.setting_id;
              this.selectedParentData = parentMatch;
            }
          }

          if (this.selectedParent) {
            // Has a parent — load parent detail and show overrides
            const ownFields = detail.inheritance_chain[0].own_fields || {};
            await this.loadParentAndBuildFields(ownFields);
          } else {
            // Materialized user profile (no parent) — edit all resolved fields directly
            this.parentDetail = detail;
            const resolved = detail.resolved || {};
            const skipKeys = new Set([
              "name", "inherits", "from", "setting_id", "instantiation",
              "compatible_printers", "compatible_printers_condition",
              "compatible_prints", "compatible_prints_condition",
              "filament_settings_id", "base_id",
            ]);
            const fields = [];
            for (const [key, val] of Object.entries(resolved)) {
              if (skipKeys.has(key)) continue;
              fields.push({
                key,
                value: formatEditValue(val),
                parentValue: val,
                modified: false,
                wrapArray: isSingleArray(val),
              });
            }
            this.fieldGroups = categorizeFields(fields);
            this.editableFields = fields;
          }
          this.step = 2;
        } catch (err) {
          alert("Failed to load profile for editing: " + err.message);
        }
      }
    },

    // Filter parent profiles by search term
    filteredParents() {
      const q = this.parentSearch.toLowerCase().trim();
      if (!q) return this.allFilaments;
      return this.allFilaments.filter(
        (f) =>
          (f.name || "").toLowerCase().includes(q) ||
          (f.setting_id || "").toLowerCase().includes(q)
      );
    },

    selectParent(profile) {
      this.selectedParent = profile.setting_id;
      this.selectedParentData = profile;
    },

    // Move to step 2: load parent detail and build editable fields
    async goStep2() {
      if (!this.selectedParent) return;
      await this.loadParentAndBuildFields({});
      this.step = 2;
    },

    // Load the parent profile detail and create the editable field list
    async loadParentAndBuildFields(existingOverrides) {
      try {
        this.parentDetail = await api(
          `/profiles/filaments/${encodeURIComponent(this.selectedParent)}`
        );
      } catch (err) {
        alert("Failed to load parent profile: " + err.message);
        return;
      }

      const resolved = this.parentDetail.resolved || {};

      // Show ALL resolved keys from the parent profile so the user can
      // override anything. Existing overrides are merged in too.
      const allKeys = new Set([
        ...Object.keys(resolved),
        ...Object.keys(existingOverrides),
      ]);

      // Skip metadata keys that shouldn't be edited directly
      const skipKeys = new Set([
        "name", "inherits", "from", "setting_id", "instantiation",
        "compatible_printers", "compatible_printers_condition",
        "compatible_prints", "compatible_prints_condition",
        "filament_settings_id", "base_id",
      ]);

      // Build flat field list
      const fields = [];
      for (const key of allKeys) {
        if (skipKeys.has(key)) continue;
        const parentVal = resolved[key];
        const hasOverride = key in existingOverrides;
        const overrideVal = hasOverride ? existingOverrides[key] : parentVal;
        fields.push({
          key,
          value: formatEditValue(overrideVal),
          parentValue: parentVal,
          modified: hasOverride,
          wrapArray: isSingleArray(parentVal),
        });
      }

      // Group fields by category using key patterns
      this.fieldGroups = categorizeFields(fields);
      this.editableFields = fields; // keep flat list for buildPayload
    },

    // Mark a field as modified when the user changes it
    updateField(field, newValue) {
      field.value = newValue;
      field.modified = newValue !== formatEditValue(field.parentValue);
    },

    // Reset a field back to its parent value
    resetField(field) {
      field.value = formatEditValue(field.parentValue);
      field.modified = false;
    },

    // Move to step 3: preview the import
    async goStep3() {
      if (!this.profileName) return;
      this.step = 3;
      this.previewLoading = true;
      this.previewError = null;
      this.preview = null;

      // Build the import payload
      const payload = this.buildPayload();

      try {
        this.preview = await api("/profiles/filaments/resolve-import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      } catch (err) {
        this.previewError = "Preview failed: " + err.message;
      } finally {
        this.previewLoading = false;
      }
    },

    // Build the import payload from current editor state
    buildPayload() {
      // For materialized profiles (no parent), include ALL fields since
      // there's no inherits chain to resolve defaults from.
      const includeAll = !this.selectedParentData;
      const overrides = {};
      for (const field of this.editableFields) {
        if (field.modified || includeAll) {
          let val = field.value;
          // Re-wrap into single-element array if the parent used that format
          if (field.wrapArray) {
            overrides[field.key] = [val];
          } else {
            // Try to parse as JSON for arrays/objects, otherwise use string
            try {
              val = JSON.parse(val);
            } catch {
              // keep as string
            }
            overrides[field.key] = val;
          }
        }
      }

      const payload = {
        name: this.profileName,
        inherits: this.selectedParentData
          ? this.selectedParentData.name
          : undefined,
        ...overrides,
      };

      // skipKeys hides compat metadata; without a parent to inherit it,
      // carry it manually so machine filtering still matches post-save.
      if (!this.selectedParentData) {
        const resolved = this.parentDetail?.resolved || {};
        if (resolved.compatible_printers !== undefined) {
          payload.compatible_printers = resolved.compatible_printers;
        }
        if (resolved.compatible_printers_condition !== undefined) {
          payload.compatible_printers_condition =
            resolved.compatible_printers_condition;
        }
      }

      return payload;
    },

    // Save the filament profile
    async save() {
      if (!this.preview) return;
      this.saving = true;
      this.saveError = null;

      try {
        // For edits: delete the old profile first
        if (this.editingId) {
          await api(`/profiles/filaments/${encodeURIComponent(this.editingId)}`, {
            method: "DELETE",
          });
        }

        // POST the raw payload built from form state. The preview's
        // resolved_profile is informational only — the saved file is
        // the raw form (with `inherits` preserved).
        await api("/profiles/filaments", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.buildPayload()),
        });

        // Navigate back to the filament list
        window.location.hash = "#/filaments?filter=user";
      } catch (err) {
        this.saveError = "Save failed: " + err.message;
      } finally {
        this.saving = false;
      }
    },

    async saveAsCopy() {
      if (!this.profileName) return;
      const newName = prompt("Name for the copy:", this.profileName + " (Copy)");
      if (!newName || !newName.trim()) return;

      this.saving = true;
      this.saveError = null;

      try {
        const copyPayload = { ...this.buildPayload(), name: newName.trim() };
        delete copyPayload.setting_id;
        delete copyPayload.filament_id;
        // Round-trip through resolve-import for validation (parent
        // existence, materialization errors), but POST the raw copy
        // payload directly — the saved file is the raw form.
        await api("/profiles/filaments/resolve-import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(copyPayload),
        });

        await api("/profiles/filaments", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(copyPayload),
        });

        window.location.hash = "#/filaments?filter=user";
        // Force reload after the list component has re-mounted
        setTimeout(() => window.dispatchEvent(new Event("machine-filter-changed")), 100);
      } catch (err) {
        this.saveError = "Save as copy failed: " + err.message;
      } finally {
        this.saving = false;
      }
    },

    cancel() {
      window.location.hash = "#/filaments";
    },

    // Re-export formatValue for template use
    formatValue,
  };
}

/**
 * Shared import-from-JSON modal used by the Filaments and Processes list views.
 * category: "filaments" | "processes"
 * onImported: callback invoked with the saved-profile response on success
 */
function importProfileModal(category, onImported) {
  return {
    open: false,
    file: null,
    fileName: "",
    rawPayload: null,

    preview: null,
    previewLoading: false,
    previewError: null,
    editedName: "",

    saving: false,
    saveError: null,
    collisionConfirm: false,

    endpoints() {
      return {
        resolve: `/profiles/${category}/resolve-import`,
        save: `/profiles/${category}`,
      };
    },

    label() {
      return category === "filaments" ? "Filament" : "Process";
    },

    reset() {
      this.file = null;
      this.fileName = "";
      this.rawPayload = null;
      this.preview = null;
      this.previewLoading = false;
      this.previewError = null;
      this.editedName = "";
      this.saving = false;
      this.saveError = null;
      this.collisionConfirm = false;
    },

    show() {
      this.reset();
      this.open = true;
      // The file input lives in `x-show` markup, so its DOM value
      // persists across opens. Clear it so the next pick fires a
      // `change` event even if the user re-selects the same file.
      this.$nextTick(() => {
        if (this.$refs.fileInput) this.$refs.fileInput.value = "";
      });
    },

    cancel() {
      this.open = false;
      this.reset();
    },

    async onFilePicked(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      this.fileName = file.name;
      this.preview = null;
      this.previewError = null;
      this.saveError = null;
      this.collisionConfirm = false;

      let text;
      try {
        text = await file.text();
      } catch (err) {
        this.previewError = `Could not read file: ${err.message}`;
        return;
      }

      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch (err) {
        this.previewError = `Selected file is not valid JSON: ${err.message}`;
        return;
      }

      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        this.previewError = "Expected a JSON object at the top level.";
        return;
      }
      if (!parsed.name || typeof parsed.name !== "string") {
        this.previewError = "Profile JSON must contain a 'name' field.";
        return;
      }

      const detected = detectProfileCategory(parsed);
      const expected = category === "filaments" ? "filament" : "process";
      if (detected && detected !== expected) {
        const otherLabel = detected === "filament" ? "Filaments" : "Processes";
        this.previewError =
          `This looks like a ${detected} profile. Use the Import button on the ${otherLabel} page.`;
        return;
      }

      this.rawPayload = parsed;
      this.editedName = parsed.name;
      this.previewLoading = true;
      try {
        this.preview = await api(this.endpoints().resolve, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(parsed),
        });
      } catch (err) {
        this.previewError = err.message;
      } finally {
        this.previewLoading = false;
      }
    },

    async applyRename() {
      if (!this.rawPayload || !this.preview) return;
      const next = (this.editedName || "").trim();
      if (!next) {
        this.editedName = this.preview.name;
        this.previewError = "Name cannot be empty.";
        return;
      }
      if (next === this.preview.name) return;

      // Drop IDs so the backend re-derives them from the new name.
      const payload = { ...this.rawPayload, name: next };
      delete payload.setting_id;
      delete payload.filament_id;

      this.previewLoading = true;
      this.previewError = null;
      this.saveError = null;
      this.collisionConfirm = false;
      try {
        this.preview = await api(this.endpoints().resolve, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        this.rawPayload = payload;
        this.editedName = this.preview.name;
      } catch (err) {
        this.previewError = err.message;
      } finally {
        this.previewLoading = false;
      }
    },

    async submit(replace = false) {
      if (!this.preview) return;
      this.saving = true;
      this.saveError = null;

      const url = replace
        ? `${this.endpoints().save}?replace=true`
        : this.endpoints().save;

      let resp;
      try {
        resp = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // POST the raw uploaded JSON (with `inherits` preserved).
          // The preview's `resolved_profile` is informational only;
          // the saved file is the raw form.
          body: JSON.stringify(this.rawPayload),
        });
      } catch (err) {
        this.saveError = err.message;
        this.saving = false;
        return;
      }

      let body = null;
      try {
        body = await resp.json();
      } catch {
        /* empty body */
      }

      if (resp.status === 409) {
        this.collisionConfirm = true;
        this.saving = false;
        return;
      }
      if (!resp.ok) {
        this.saveError = (body && body.error) || resp.statusText || "Save failed";
        this.saving = false;
        return;
      }

      this.saving = false;
      this.open = false;
      this.reset();
      if (typeof onImported === "function") {
        onImported(body);
      }
    },

    async replaceConfirmed() {
      this.collisionConfirm = false;
      await this.submit(true);
    },

    cancelCollision() {
      this.collisionConfirm = false;
    },
  };
}
