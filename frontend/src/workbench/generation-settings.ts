export interface GenerationField {
  type?: string;
  label?: string;
  value?: unknown;
  enum?: unknown[];
  multiline?: boolean;
  max_length?: number;
  minimum?: number;
  maximum?: number;
  step?: number;
  hint?: string;
  runtime_parameter?: string;
  disables_references_when?: unknown;
  condition?: Record<string, unknown>;
  [key: string]: unknown;
}

type Translator = (key: string, params?: Record<string, unknown>) => string;
type ChangeListener = (() => void) | null;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, unknown> = {},
  children: (Node | string | null | undefined)[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs)) {
    if (val === undefined || val === null) continue;
    if (key === "className") node.className = String(val);
    else if (key === "dataset") {
      for (const [dataKey, dataValue] of Object.entries(val as Record<string, unknown>)) {
        node.dataset[dataKey] = String(dataValue ?? "");
      }
    } else node.setAttribute(key, String(val));
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child);
  }
  return node;
}

interface SettingControl {
  field: GenerationField;
  row: HTMLElement;
  enabled: HTMLInputElement;
  input: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
}

/** 供应商生成参数编辑器：勾选临时覆盖，值与条件显隐与 vanilla 行为一致。 */
export class GenerationSettingsEditor {
  fields: Record<string, GenerationField> = {};
  controls = new Map<string, SettingControl>();
  disabled = false;
  onChange: ChangeListener = null;

  constructor(
    public container: HTMLElement,
    private t: Translator,
  ) {}

  static valid(value: unknown, field: GenerationField): boolean {
    if (field.type === "boolean" && typeof value !== "boolean") return false;
    if (field.type === "integer" && !Number.isSafeInteger(value)) return false;
    if (field.type === "number" && (typeof value !== "number" || !Number.isFinite(value)))
      return false;
    if (
      field.type === "string" &&
      (typeof value !== "string" || value.length > (field.max_length as number))
    ) {
      return false;
    }
    if (field.enum && !(field.enum as unknown[]).includes(value)) return false;
    if (
      typeof value === "number" &&
      (value < ((field.minimum as number | undefined) ?? -Infinity) ||
        value > ((field.maximum as number | undefined) ?? Infinity))
    ) {
      return false;
    }
    return true;
  }

  render(fields: Record<string, GenerationField>, saved: Record<string, unknown> = {}): boolean {
    this.fields = fields || {};
    this.controls.clear();
    this.container.replaceChildren();
    let dropped = false;
    for (const [name, field] of Object.entries(this.fields)) {
      if (["resolution", "aspect_ratio"].includes(name)) continue;
      const row = el("div", { className: "provider-parameter", dataset: { parameter: name } });
      const enabled = el("input", {
        type: "checkbox",
        className: "comic-checkbox",
        "aria-label": this.t("临时覆盖{v0}", { v0: field.label }),
      }) as HTMLInputElement;
      const label = el("label", { className: "provider-parameter-toggle" }, [enabled, field.label]);
      row.appendChild(label);
      row.appendChild(el("span", { className: "provider-parameter-key" }, [name]));
      const attrs = { id: `parameter-${name}`, "aria-label": field.label };
      let input: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
      if (field.type === "boolean" || Array.isArray(field.enum)) {
        const select = el("select", { ...attrs, className: "comic-select" }) as HTMLSelectElement;
        const options = field.type === "boolean" ? [true, false] : (field.enum as unknown[]);
        for (const value of options) {
          select.appendChild(
            el("option", { value: String(value) }, [
              field.type === "boolean"
                ? value
                  ? this.t("开启")
                  : this.t("关闭")
                : value === ""
                  ? this.t("默认")
                  : String(value),
            ]),
          );
        }
        input = select;
      } else if (field.multiline) {
        input = el("textarea", {
          ...attrs,
          className: "comic-input",
          rows: 3,
          maxlength: field.max_length,
        }) as unknown as HTMLTextAreaElement;
      } else {
        input = el("input", {
          ...attrs,
          className: "comic-input",
          type: ["integer", "number"].includes(field.type as string) ? "number" : "text",
          min: field.minimum,
          max: field.maximum,
          step: field.type === "integer" ? 1 : (field.step ?? "any"),
          maxlength: field.max_length,
        }) as HTMLInputElement;
      }
      const hasSaved = Object.prototype.hasOwnProperty.call(saved, name);
      const validSaved = hasSaved && GenerationSettingsEditor.valid(saved[name], field);
      enabled.checked = validSaved;
      if (hasSaved && !validSaved) dropped = true;
      input.value = String(validSaved ? saved[name] : (field.value ?? ""));
      row.appendChild(input);
      if (field.hint) {
        row.appendChild(
          el("details", { className: "provider-parameter-help" }, [
            el("summary", {}, [this.t("参数说明")]),
            el("p", {}, [field.hint]),
          ]),
        );
      }
      const changed = () => {
        this.updateConditions();
        this.onChange?.();
      };
      enabled.addEventListener("change", changed);
      input.addEventListener("input", changed);
      this.controls.set(name, { field, row, enabled, input });
      this.container.appendChild(row);
    }
    if (Object.keys(saved).some((name) => !this.controls.has(name))) dropped = true;
    this.updateConditions();
    return dropped;
  }

  value(control: SettingControl): unknown {
    const { field, input } = control;
    if (field.type === "boolean") return (input as HTMLInputElement).value === "true";
    if (["integer", "number"].includes(field.type as string)) {
      return (input as HTMLInputElement).value.trim()
        ? Number((input as HTMLInputElement).value)
        : NaN;
    }
    return input.value;
  }

  effectiveValues(): Record<string, unknown> {
    const values = Object.fromEntries(
      Object.entries(this.fields).map(([name, field]) => [name, field.value]),
    );
    for (const [name, control] of this.controls) {
      if (control.enabled.checked) values[name] = this.value(control);
    }
    return values;
  }

  matchesCondition(
    field?: GenerationField,
    values: Record<string, unknown> = this.effectiveValues(),
  ): boolean {
    return Object.entries(field?.condition || {}).every(([name, value]) => values[name] === value);
  }

  updateConditions() {
    const values = this.effectiveValues();
    for (const control of this.controls.values()) {
      control.row.hidden = !this.matchesCondition(control.field, values);
      control.enabled.disabled = this.disabled;
      control.input.disabled = this.disabled || !control.enabled.checked || control.row.hidden;
    }
  }

  setDisabled(disabled: boolean) {
    this.disabled = disabled;
    this.updateConditions();
  }

  overrides({ validate = true, includeHidden = false } = {}): Record<string, unknown> {
    const values: Record<string, unknown> = {};
    for (const [name, control] of this.controls) {
      if (!control.enabled.checked || (!includeHidden && control.row.hidden)) continue;
      const value = this.value(control);
      if (!GenerationSettingsEditor.valid(value, control.field)) {
        if (validate) {
          control.input.focus();
          throw new Error(this.t("请检查{v0}的值与范围", { v0: control.field.label }));
        }
        continue;
      }
      values[name] = value;
    }
    return values;
  }
}
