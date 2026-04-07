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
 */
function categorizeFields(fields) {
  const categories = [
    { name: "Material",     test: (k) => /^filament_(type|vendor|cost|density|colour|color|diameter|weight)/.test(k) },
    { name: "Temperature",  test: (k) => /temperature|^heat_bed|^chamber_temp/.test(k) },
    { name: "Retraction",   test: (k) => /retract|deretract|wipe/.test(k) },
    { name: "Speed",        test: (k) => /speed|volumetric|acceleration/.test(k) && !/fan/.test(k) },
    { name: "Flow & Pressure", test: (k) => /flow|pressure|advance/.test(k) },
    { name: "Cooling & Fan",   test: (k) => /fan|cooling|cool/.test(k) },
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
      } catch (err) {
        console.error("Failed to load machines for filter:", err);
      }
    },

    selectMachine(settingId) {
      this.selectedMachineId = settingId;
      // Re-trigger current list view to reload with filter
      window.dispatchEvent(new Event("machine-filter-changed"));
    },

    init() {
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

    get filteredItems() {
      const q = this.search.toLowerCase().trim();
      if (!q) return this.items;
      return this.items.filter(
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
      const overrides = {};
      for (const field of this.editableFields) {
        if (field.modified) {
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

      return {
        name: this.profileName,
        inherits: this.selectedParentData
          ? this.selectedParentData.name
          : undefined,
        ...overrides,
      };
    },

    // Save the filament profile
    async save() {
      if (!this.preview || !this.preview.resolved_payload) return;
      this.saving = true;
      this.saveError = null;

      try {
        // For edits: delete the old profile first
        if (this.editingId) {
          await api(`/profiles/filaments/${encodeURIComponent(this.editingId)}`, {
            method: "DELETE",
          });
        }

        // POST the resolved payload to create the new profile
        await api("/profiles/filaments", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.preview.resolved_payload),
        });

        // Navigate back to the filament list
        window.location.hash = "#/filaments?filter=user";
      } catch (err) {
        this.saveError = "Save failed: " + err.message;
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
