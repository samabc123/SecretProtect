package handler

import (
	"net/http"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/labadmission"
	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	middleware "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/gin-gonic/gin"
)

func (h *AuthHandler) LabAdmission() *labadmission.Engine { return h.authService.LabAdmission() }

func (h *AuthHandler) LabAdmissionConfig(c *gin.Context) {
	response.Success(c, gin.H{"enabled": h.LabAdmission() != nil, "password_min": 6, "password_max": 128, "fixed_concurrency": labadmission.FixedConcurrency})
}

func (h *AuthHandler) LabAdminRequests(c *gin.Context) {
	if h.LabAdmission() == nil {
		response.BadRequest(c, "实验室准入未启用。")
		return
	}
	rows, err := h.LabAdmission().AdminRequests(c.Request.Context())
	if err != nil {
		response.InternalError(c, "准入记录暂不可用。")
		return
	}
	c.Header("Cache-Control", "no-store")
	response.Success(c, gin.H{"items": rows})
}

func (h *AuthHandler) LabDelegate(c *gin.Context) {
	actor, ok := middleware.GetAuthSubjectFromContext(c)
	if h.LabAdmission() == nil || !ok || c.GetString("auth_method") != "jwt" {
		c.AbortWithStatusJSON(403, gin.H{"code": 403, "message": "需要管理员登录会话。"})
		return
	}
	var input labadmission.Input
	if err := c.ShouldBindJSON(&input); err != nil {
		response.BadRequest(c, "请提交有效的用户名和邮箱。")
		return
	}
	if fields := input.Validate(); len(fields) > 0 {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"code": 422, "message": "请检查注册字段。", "field_errors": fields})
		return
	}
	status, err := h.authService.DelegateLabRegistration(c.Request.Context(), input, actor.UserID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, status)
}

func (h *AuthHandler) LabInvite(c *gin.Context) {
	actor, ok := middleware.GetAuthSubjectFromContext(c)
	if h.LabAdmission() == nil || !ok || c.GetString("auth_method") != "jwt" {
		c.AbortWithStatusJSON(403, gin.H{"code": 403, "message": "需要管理员登录会话。"})
		return
	}
	link, err := h.LabAdmission().Invite(c.Request.Context(), c.Param("request_id"), actor.UserID)
	if err != nil {
		c.AbortWithStatusJSON(409, gin.H{"code": 409, "message": "仅可为已批准且等待激活的申请生成邀请。"})
		return
	}
	c.Header("Cache-Control", "no-store")
	response.Success(c, gin.H{"activation_path": link, "expires_in": 900})
}

func (h *AuthHandler) LabActivate(c *gin.Context) {
	if h.LabAdmission() == nil {
		response.BadRequest(c, "实验室准入未启用。")
		return
	}
	var input struct {
		Token    string `json:"token" binding:"required,len=64"`
		Password string `json:"password" binding:"required,min=6,max=128"`
	}
	if err := c.ShouldBindJSON(&input); err != nil {
		response.BadRequest(c, "请检查邀请链接和密码长度。")
		return
	}
	if err := h.LabAdmission().Activate(c.Request.Context(), input.Token, input.Password); err != nil {
		c.AbortWithStatusJSON(403, gin.H{"code": 403, "message": "邀请已使用、已过期或当前准入无效。"})
		return
	}
	c.Header("Cache-Control", "no-store")
	response.Success(c, gin.H{"message": "账号已激活，请登录。"})
}

func (h *AuthHandler) labRegister(c *gin.Context, req RegisterRequest) {
	input := labadmission.Input{RequestID: req.RequestID, Username: req.Username, Email: req.Email}
	fields := input.Validate()
	if length := utf8.RuneCountInString(req.Password); length < 6 || length > 128 {
		fields["password"] = "密码须为6至128个字符。"
	}
	if len(fields) > 0 {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"code": 422, "message": "请检查注册字段。", "field_errors": fields})
		return
	}
	result, err := h.authService.SubmitLabRegistration(c.Request.Context(), input, req.Password, req.VerifyCode, req.InvitationCode)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	c.JSON(http.StatusAccepted, gin.H{"code": 0, "data": gin.H{"application": result}})
}

func (h *AuthHandler) LabApplicationStatus(c *gin.Context) {
	var req struct {
		RequestID string `json:"request_id" binding:"required,min=16,max=100"`
		Email     string `json:"email" binding:"required,email,max=180"`
		Password  string `json:"password" binding:"required,max=128"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "请填写申请编号、邮箱和密码。")
		return
	}
	result, err := h.authService.LabApplicationStatus(c.Request.Context(), req.RequestID, req.Email, req.Password)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}
