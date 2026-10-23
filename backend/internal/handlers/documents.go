package handlers

import (
	"database/sql"
	"fmt"
	"io"
	"net/http"

	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/ingest"
	"opspilot/backend/internal/middleware"
)

// UploadDocument 上传文档（异步摄取）。
func UploadDocument(c *gin.Context) {
	userID, _ := c.Get("user_id")
	ownerID, _ := userID.(string)
	if ownerID == "" {
		middleware.AbortWithError(c, http.StatusUnauthorized, "UNAUTHORIZED", "无法识别用户")
		return
	}

	file, header, err := c.Request.FormFile("file")
	if err != nil {
		middleware.AbortWithError(c, http.StatusBadRequest, "INVALID_REQUEST", "请上传 file 字段")
		return
	}
	defer file.Close()

	data, err := io.ReadAll(file)
	if err != nil {
		middleware.AbortWithError(c, http.StatusBadRequest, "INVALID_REQUEST", "读取文件失败")
		return
	}

	db := c.MustGet("db").(*sql.DB)
	doc, err := ingest.CreateDocument(db, ownerID, header.Filename, data)
	if err != nil {
		if httpErr, ok := err.(*ingest.IngestHTTPError); ok {
			middleware.AbortWithError(c, httpErr.Status, httpErr.Code, httpErr.Error())
			return
		}
		middleware.AbortWithError(c, http.StatusInternalServerError, "INTERNAL_ERROR", err.Error())
		return
	}

	resp := gin.H{
		"doc_id":          doc.ID,
		"ingest_status":   doc.Status,
		"deduplicated":    doc.Dedup,
		"poll_url":        fmt.Sprintf("/api/v1/documents/%s", doc.ID),
		"title":           doc.Title,
		"byte_size":       doc.ByteSize,
		"char_count":      doc.CharCount,
	}
	if doc.Dedup {
		resp["retry_hint"] = fmt.Sprintf("POST /api/v1/documents/%s/reingest", doc.ID)
	}
	c.JSON(http.StatusAccepted, resp)
}

// GetDocument 查询文档摄取状态。
func GetDocument(c *gin.Context) {
	userID, _ := c.Get("user_id")
	ownerID, _ := userID.(string)
	docID := c.Param("doc_id")

	db := c.MustGet("db").(*sql.DB)
	doc, err := ingest.GetDocument(db, ownerID, docID)
	if err != nil {
		middleware.AbortWithError(c, http.StatusInternalServerError, "INTERNAL_ERROR", err.Error())
		return
	}
	if doc == nil {
		middleware.AbortWithError(c, http.StatusNotFound, "NOT_FOUND", "文档不存在")
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"doc_id":        doc.ID,
		"title":         doc.Title,
		"ingest_status": doc.Status,
		"error_code":    doc.ErrorCode,
		"error_msg":     doc.ErrorMsg,
		"attempt":       doc.Attempt,
		"chunk_count":   doc.ChunkCount,
		"byte_size":     doc.ByteSize,
		"char_count":    doc.CharCount,
		"created_at":    doc.CreatedAt,
	})
}

// ReingestDocument 手动重试 failed 文档。
func ReingestDocument(c *gin.Context) {
	userID, _ := c.Get("user_id")
	ownerID, _ := userID.(string)
	docID := c.Param("doc_id")

	db := c.MustGet("db").(*sql.DB)
	if err := ingest.Reingest(db, ownerID, docID); err != nil {
		if httpErr, ok := err.(*ingest.IngestHTTPError); ok {
			middleware.AbortWithError(c, httpErr.Status, httpErr.Code, httpErr.Error())
			return
		}
		middleware.AbortWithError(c, http.StatusInternalServerError, "INTERNAL_ERROR", err.Error())
		return
	}

	c.JSON(http.StatusAccepted, gin.H{
		"doc_id":        docID,
		"ingest_status": ingest.StatusPending,
		"poll_url":      fmt.Sprintf("/api/v1/documents/%s", docID),
	})
}

// DeleteDocument 删除文档（进入 deleting 状态）。
func DeleteDocument(c *gin.Context) {
	userID, _ := c.Get("user_id")
	ownerID, _ := userID.(string)
	docID := c.Param("doc_id")

	db := c.MustGet("db").(*sql.DB)
	if err := ingest.MarkDeleting(db, ownerID, docID); err != nil {
		if httpErr, ok := err.(*ingest.IngestHTTPError); ok {
			middleware.AbortWithError(c, httpErr.Status, httpErr.Code, httpErr.Error())
			return
		}
		middleware.AbortWithError(c, http.StatusInternalServerError, "INTERNAL_ERROR", err.Error())
		return
	}

	c.JSON(http.StatusAccepted, gin.H{
		"doc_id":        docID,
		"ingest_status": ingest.StatusDeleting,
	})
}
