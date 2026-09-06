"""Persistent chatbot conversations, messages, quota usage, and user memory."""
from datetime import date, datetime

from models.base import db


class ChatConversation(db.Model):
    __tablename__ = "chat_conversation"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False, default="New chat")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False, index=True,
    )

    user = db.relationship(
        "User",
        backref=db.backref(
            "chat_conversations", lazy="dynamic", cascade="all, delete-orphan",
        ),
    )
    messages = db.relationship(
        "ChatMessage", backref="conversation", lazy="dynamic",
        cascade="all, delete-orphan", order_by="ChatMessage.created_at",
    )


class ChatMessage(db.Model):
    __tablename__ = "chat_message"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("chat_conversation.id"), nullable=False, index=True,
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class UserChatDailyUsage(db.Model):
    __tablename__ = "user_chat_daily_usage"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    usage_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    question_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False,
    )
    user = db.relationship(
        "User",
        backref=db.backref("chat_daily_usage", cascade="all, delete-orphan"),
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "usage_date", name="unique_user_chat_usage_day"),
    )


class UserChatMemory(db.Model):
    __tablename__ = "user_chat_memory"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    content = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False,
    )
    user = db.relationship(
        "User",
        backref=db.backref("chat_memory", uselist=False, cascade="all, delete-orphan"),
    )
