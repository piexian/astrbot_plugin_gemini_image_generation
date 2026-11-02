<script setup lang="ts">
import { computed } from "vue";
import type { StudioTemplate, Translator } from "./types";

const props = defineProps<{
  template: StudioTemplate;
  writable: boolean;
  hubEnabled: boolean;
  t: Translator;
}>();

const emit = defineEmits<{
  use: [];
  edit: [];
  copy: [];
  upload: [];
  restore: [];
  remove: [];
  toggleAi: [value: boolean];
}>();

const builtin = computed(() => props.template.builtin === true);
const tags = computed(() => props.template.tags || []);
const defaultsText = computed(() => {
  const parts: string[] = [];
  if (props.template.defaults?.resolution) parts.push(props.template.defaults.resolution);
  if (props.template.defaults?.aspect_ratio) parts.push(props.template.defaults.aspect_ratio);
  return parts.join(" · ");
});
function referenceLabel(value?: string) {
  const t = props.t;
  if (value === "required") return t("需要参考图");
  if (value === "optional") return t("可选参考图");
  return t("不需要参考图");
}
</script>

<template>
  <article class="tpl-card comic-card">
    <div class="tpl-card-head">
      <h3 class="tpl-card-title">{{ template.name }}</h3>
      <span class="meta-pill">{{ builtin ? t("内置") : t("本地") }}</span>
    </div>
    <p class="tpl-card-desc">{{ template.ai_description || "" }}</p>
    <div class="tpl-card-tags">
      <span v-for="tag in tags" :key="tag" class="meta-pill">{{ tag }}</span>
    </div>
    <p class="tpl-card-meta">
      {{ referenceLabel(template.reference)
      }}<template v-if="defaultsText"> · {{ defaultsText }}</template>
    </p>
    <label class="tpl-card-ai">
      <input
        type="checkbox"
        class="comic-checkbox"
        :checked="template.ai_enabled !== false"
        :disabled="!writable"
        @change="emit('toggleAi', ($event.target as HTMLInputElement).checked)"
      />
      {{ t("对 AI 可用") }}
    </label>
    <div class="tpl-card-actions">
      <button type="button" class="comic-btn comic-btn--sm comic-btn--cta" @click="emit('use')">
        {{ t("填入工作台") }}
      </button>
      <button
        type="button"
        class="comic-btn comic-btn--sm comic-btn--outline"
        :disabled="!writable"
        @click="emit('edit')"
      >
        {{ t("编辑") }}
      </button>
      <button
        type="button"
        class="comic-btn comic-btn--sm comic-btn--outline"
        :disabled="!writable"
        @click="emit('copy')"
      >
        {{ t("复制") }}
      </button>
      <button
        v-if="!builtin && hubEnabled"
        type="button"
        class="comic-btn comic-btn--sm comic-btn--outline"
        :disabled="!writable"
        @click="emit('upload')"
      >
        {{ t("上传到在线模板库") }}
      </button>
      <button
        v-if="builtin"
        type="button"
        class="comic-btn comic-btn--sm comic-btn--outline"
        :disabled="!writable"
        @click="emit('restore')"
      >
        {{ t("恢复默认") }}
      </button>
      <button
        v-else
        type="button"
        class="comic-btn comic-btn--sm comic-btn--danger"
        :disabled="!writable"
        @click="emit('remove')"
      >
        {{ t("删除") }}
      </button>
    </div>
  </article>
</template>
