// 认证页新增文案成对维护，保持两种语言的 key 天然一致。
export const authExtraMessages: Record<string, readonly [string, string]> = {
  "auth.usernamePlaceholder": ["用户名", "Username"],
  "auth.usernameLength": ["用户名须为 3 至 64 个字符。", "Username must contain 3 to 64 characters."],
  "auth.passwordLength": ["密码须为 8 至 128 个字符。", "Password must contain 8 to 128 characters."],
  "auth.codeLength": ["请输入 6 位数字验证码。", "Enter a six-digit verification code."],
  "auth.captchaUnavailable": ["验证码未加载，请重新获取。", "Captcha unavailable. Refresh to try again."],
  "auth.correctFields": ["请检查下方标记的输入。", "Check the marked fields below."],
  "auth.showPassword": ["显示密码", "Show password"],
  "auth.hidePassword": ["隐藏密码", "Hide password"],
  "auth.adminAccount": ["管理员账户", "Administrator account"],
  "auth.installAuthorization": ["安装授权", "Installation authorization"],
  "auth.usernameHint": ["3 至 64 个字符。", "3 to 64 characters."],
  "auth.passwordHint": ["8 至 128 个字符，请使用独立的强密码。", "8 to 128 characters. Use a unique strong password."],
  "auth.tokenHint": ["由部署管理员提供初始化授权令牌。", "Request the bootstrap authorization token from your deployment administrator."],
  "auth.connectionPending": ["交易控制台暂时不可用，连接恢复后可继续。", "The trading console is unavailable. Resume when the connection is restored."],
  "auth.reconnecting": ["正在重试连接", "Retrying connection"],
  "auth.notFoundCode": ["404", "404"],
};
