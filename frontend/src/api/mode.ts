// 样例模式是构建时开关，界面不会偷偷回退到真实接口。
export const MOCK_MODE = import.meta.env.VITE_MOCK === "1";
