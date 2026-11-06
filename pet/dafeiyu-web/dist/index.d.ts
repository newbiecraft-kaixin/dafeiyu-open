interface PetEndpoints {
    /** 碎碎念后端：GET → { line: string }；失败自动降级本地台词池 */
    whisper?: string;
    /** 聊天后端：POST {messages:[{role,content}...]} → SSE（data:{"d":增量}|{"e":错误}|[DONE]） */
    chat?: string;
}
type CornerPosition = 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left';
interface DafeiyuOptions {
    /** 素材目录 URL（末尾带不带 / 都行）。默认 jsDelivr，发布后生效 */
    assetBase?: string;
    /** 宠物显示宽度 px */
    size?: number;
    /** 初始位置：角落名，或归一化中心坐标 {x,y}（0~1） */
    position?: CornerPosition | {
        x: number;
        y: number;
    };
    /** 容器 z-index */
    zIndex?: number;
    /** 追加台词（内置池之外）。传空数组则只用内置池 */
    lines?: string[];
    /** 自动说话间隔（秒），实际 ±30% 抖动 */
    whisperIntervalSec?: number;
    /** 宠物显示名（菜单头部、聊天面板标题），默认「大肥鱼」 */
    name?: string;
    /** 视口 <768px 时是否显示，默认隐藏 */
    enabledOnMobile?: boolean;
    /** whisper 请求是否附带当前页面标题/路径（后端可据此说应景台词），默认开 */
    pageContext?: boolean;
    /** 语音朗读（浏览器 TTS）：碎碎念与聊天回复开口读出来，默认关；访客可在右键菜单切换 */
    voice?: boolean;
    /** AI 后端地址：配了才有碎碎念拉取和双击聊天面板，失败降级本地台词池 */
    endpoints?: PetEndpoints;
}

interface DafeiyuInstance {
    /** 移除宠物并清理全部监听与定时器 */
    destroy(): void;
    /** 立刻说一句话（不传则从台词池随机） */
    say(text?: string): void;
    /** 立刻播一段动画（语义名见包内 anims.ts / README） */
    play(name: string): void;
    /** 喂食（好感 +2，随机播放吃相动画） */
    feed(): void;
    /** 语音朗读开关（右键菜单同款入口） */
    setVoice(on: boolean): void;
    /** 打开聊天面板（需配置 endpoints.chat，否则 no-op） */
    openChat(): void;
    closeChat(): void;
    /** 打开五子棋（本地小游戏，无需后端） */
    openGame(): void;
    closeGame(): void;
}
/**
 * 挂载宠物。同一页面重复调用会先销毁上一个实例。
 *
 * ```html
 * <script src="https://cdn.jsdelivr.net/npm/dafeiyu/dist/dafeiyu.iife.js"></script>
 * <script>Dafeiyu.mount({ assetBase: '/pet-assets/' })</script>
 * ```
 */
declare function mount(options?: DafeiyuOptions): DafeiyuInstance;
declare const _default: {
    mount: typeof mount;
};

export { type CornerPosition, type DafeiyuInstance, type DafeiyuOptions, type PetEndpoints, _default as default, mount };
