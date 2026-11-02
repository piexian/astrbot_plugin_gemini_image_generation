export interface StudioPreferencesBridge {
  get: (path: string) => Promise<unknown>;
  post: (path: string, body?: unknown) => Promise<unknown>;
}

type Translator = (key: string, params?: Record<string, unknown>) => string;
type StatusListener = ((status: string) => void) | null;

interface CandidateIdentity {
  [key: string]: unknown;
  candidate_id: unknown;
  provider?: unknown;
  model?: unknown;
}

type CandidateIdentityLike = {
  candidate_id: unknown;
  provider?: unknown;
  model?: unknown;
};

type SavedCandidate = Record<string, unknown> & CandidateIdentity;

interface PreferencesData {
  selected: CandidateIdentity | null;
  candidates: Record<string, SavedCandidate>;
}

/** 浏览器参数记忆：优先 localStorage，沙箱 iframe 降级为宿主桥接 Cookie。 */
export class StudioPreferences {
  key = "gemini-image-generation.studio.preferences.v1";
  data: PreferencesData = { selected: null, candidates: {} };
  revision = 0;
  remote = false;
  ready = false;
  storage: Storage | null = null;
  onStatus: StatusListener = null;

  constructor(
    private bridge: StudioPreferencesBridge,
    private t: Translator,
  ) {
    try {
      this.storage = window.localStorage;
    } catch {
      // AstrBot 的沙箱 iframe 没有存储权限，通过父页桥接使用浏览器专属 Cookie。
      this.remote = true;
    }
    if (this.storage) {
      try {
        const saved = JSON.parse(this.storage.getItem(this.key) || "null") as
          | (PreferencesData & { candidates?: unknown })
          | null;
        if (saved && typeof saved.candidates === "object" && !Array.isArray(saved.candidates)) {
          this.data = { selected: saved.selected || null, candidates: saved.candidates || {} };
        }
      } catch (error) {
        console.warn("[Studio] 无法读取浏览器参数记忆:", error);
      }
    }
    this.ready = !this.remote;
  }

  async load() {
    if (this.remote) {
      try {
        const value = (await this.bridge.get("webui/preferences")) as {
          preferences?: PreferencesData;
          revision?: number;
        } | null;
        this.data = value!.preferences as PreferencesData;
        this.revision = value!.revision as number;
      } catch (error) {
        console.warn("[Studio] 无法恢复浏览器参数记忆:", error);
        this.onStatus?.(this.t("参数记忆暂不可用"));
      }
    }
    this.ready = true;
  }

  matches(saved: SavedCandidate | null | undefined, candidate: CandidateIdentityLike): boolean {
    if (!saved) return false;
    return (
      saved.candidate_id === candidate.candidate_id &&
      saved.provider === candidate.provider &&
      saved.model === candidate.model
    );
  }

  identity(candidate: CandidateIdentityLike): SavedCandidate {
    return {
      candidate_id: candidate.candidate_id,
      provider: candidate.provider,
      model: candidate.model,
    };
  }

  get(candidate: CandidateIdentityLike): SavedCandidate | null {
    const saved = this.data.candidates[candidate.candidate_id as string];
    return saved && this.matches(saved, candidate) ? saved : null;
  }

  select(candidate: CandidateIdentityLike) {
    this.data.selected = this.identity(candidate);
    this.flush();
  }

  remember(candidate: CandidateIdentityLike, settings: Record<string, unknown>) {
    this.data.selected = this.identity(candidate);
    this.data.candidates[candidate.candidate_id as string] = {
      ...this.identity(candidate),
      ...settings,
    };
    this.flush();
  }

  reset(candidate: CandidateIdentityLike) {
    delete this.data.candidates[candidate.candidate_id as string];
    this.flush();
  }

  flush() {
    if (!this.ready) return;
    if (this.remote) {
      const revision = (this.revision = Math.max(this.revision + 1, Date.now() * 1000));
      this.onStatus?.(this.t("正在记忆参数…"));
      void this.bridge
        .post("webui/preferences", {
          revision,
          preferences: JSON.parse(JSON.stringify(this.data)),
        })
        .then(() => {
          if (revision === this.revision) this.onStatus?.(this.t("已记住当前选择"));
        })
        .catch((error: unknown) => {
          if (revision === this.revision) this.onStatus?.(this.t("参数记忆暂不可用"));
          console.warn("[Studio] 无法保存浏览器参数记忆:", error);
        });
      return;
    }
    try {
      this.storage?.setItem(this.key, JSON.stringify(this.data));
      this.onStatus?.(this.t("已记住当前选择"));
    } catch (error) {
      this.onStatus?.(this.t("参数记忆暂不可用"));
      console.warn("[Studio] 无法保存浏览器参数记忆:", error);
    }
  }
}
